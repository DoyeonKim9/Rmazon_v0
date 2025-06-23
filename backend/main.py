from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import StandardScaler, OneHotEncoder
from sklearn.linear_model import Ridge
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction import text
import os
import shutil
from typing import List, Optional, Dict
import numpy as np

# --- Pydantic 모델 정의 ---
# 요청 본문의 데이터 구조를 정의합니다.
class Keyword(BaseModel):
    word: str
    count: int

class PredictionRequest(BaseModel):
    price: float
    category: str

# 응답 본문의 데이터 구조를 정의합니다.
class PredictionResponse(BaseModel):
    predicted_star: float
    predicted_review_count: int # 리뷰 개수 request에서 response로 수정
    price_percentile: float
    review_count_percentile: float
    rating_percentile: float

class SimilarityRequest(BaseModel):
    description: str
    price: float
    discount_percentage: float
    category: str

class Product(BaseModel):
    product_id: str
    product_name: str

class ReviewAnalysis(BaseModel):
    overall_sentiment: str
    sentiment_distribution: Dict[str, int]
    top_keywords: List[Keyword]
    negative_concerns: List[str]
    summary: str
    review_count: int

class SimilarityResult(BaseModel):
    product_id: str
    product_name: str
    similarity: float
    discounted_price: float
    rating: float
    rating_count: int
    review_analysis: ReviewAnalysis

class PriceRangeResponse(BaseModel):
    min_price: float
    max_price: float

class DistributionBin(BaseModel):
    name: str
    count: int

class CategoryStatsResponse(BaseModel):
    min_price: float
    max_price: float
    min_review_count: float
    max_review_count: float
    price_distribution: List[DistributionBin]
    review_count_distribution: List[DistributionBin]
    rating_distribution: List[DistributionBin]

# --- FastAPI 애플리케이션 설정 ---
app = FastAPI()

# CORS 미들웨어 추가: Next.js 앱(http://localhost:3000)에서의 요청을 허용합니다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 전역 변수: 모델, 데이터, 전처리기 ---
ml_pipe = None
tfidf_vectorizer = None
tfidf_matrix = None
df_products = pd.DataFrame() # 상품 메타데이터 및 유사도 분석용
df_reviews = pd.DataFrame() # 상품별 개별 리뷰 저장용
DATA_FILE_PATH = os.path.join("data", "cleaned_amazon_0519.csv")

# --- 핵심 로직: 데이터 로딩 및 모델 학습 ---
def load_data_and_train_models():
    global ml_pipe, count_pipe, tfidf_vectorizer, tfidf_matrix, df_products, df_reviews # 함수 내 리뷰 수 예측 모델학습 추가
    
    if not os.path.exists(DATA_FILE_PATH):
        print(f"⚠️ 데이터 파일이 존재하지 않습니다: {DATA_FILE_PATH}")
        df_products, df_reviews = pd.DataFrame(), pd.DataFrame()
        return

    df_raw = pd.read_csv(DATA_FILE_PATH)
    
    required_columns = ['product_id', 'product_name', 'review_title', 'review_content', 'discounted_price', 'rating_count', 'category_cleaned', 'rating']
    if any(col not in df_raw.columns for col in required_columns):
        raise ValueError(f"필수 컬럼 중 일부가 누락되었습니다.")

    # --- 1. 기본 클리닝 및 타입 변환 ---
    df_raw['review_title'] = df_raw['review_title'].fillna('')
    df_raw['review_content'] = df_raw['review_content'].fillna('')
    for col in ['discounted_price', 'rating_count', 'rating']:
        df_raw[col] = pd.to_numeric(df_raw[col], errors='coerce')
    df_raw.dropna(subset=['product_id', 'discounted_price', 'rating_count', 'rating', 'category_cleaned'], inplace=True)

    # --- 2. 리뷰 분리 및 df_reviews 생성 ---
    reviews_list = []
    for _, row in df_raw.iterrows():
        # review_content를 쉼표로 분리하여 개별 리뷰 리스트 생성
        # 내용이 없는 빈 리뷰는 제외
        contents = [c.strip() for c in str(row['review_content']).split(',') if c.strip()]
        for content_part in contents:
            reviews_list.append({
                'product_id': row['product_id'],
                'review_text': content_part
            })
    
    df_reviews = pd.DataFrame(reviews_list)
    
    if df_reviews.empty:
        print("⚠️ 리뷰 데이터를 분리한 후 처리할 데이터가 없습니다.")
        df_products = pd.DataFrame()
        return

    # --- 3. 유사도 분석용 df_products 생성 ---
    # 상품별로 분리된 리뷰 텍스트를 다시 하나로 합쳐 'combined_text' 생성
    df_aggregated_reviews = df_reviews.groupby('product_id')['review_text'].apply(lambda x: ' '.join(x)).reset_index()
    df_aggregated_reviews.rename(columns={'review_text': 'combined_text'}, inplace=True)

    # 원본 데이터에서 상품 메타데이터(리뷰 제외)를 가져와 결합
    df_meta = df_raw.drop(columns=['review_title', 'review_content']).drop_duplicates(subset=['product_id']).set_index('product_id')
    df_products = df_aggregated_reviews.merge(df_meta, on='product_id', how='left')
    
    # 모델 학습에 필요한 컬럼이 모두 있는지 최종 확인
    df_products.dropna(subset=['discounted_price', 'rating_count', 'rating', 'category_cleaned'], inplace=True)
    
    if df_products.empty:
        print("⚠️ 최종 상품 데이터가 비어있습니다. 모델 학습을 건너뜁니다.")
        return
        
    # --- 4. 모델 학습 ---
    # 릿지 회귀 모델 학습 (별점 예측용)
    X_ridge = df_products[['discounted_price', 'rating_count', 'category_cleaned']]
    y_ridge = df_products['rating']
    numeric_features = ['discounted_price', 'rating_count']
    categorical_features = ['category_cleaned']
    preprocessor = ColumnTransformer(
        transformers=[
            ('num', StandardScaler(), numeric_features),
            ('cat', OneHotEncoder(handle_unknown='ignore'), categorical_features)])
    ml_pipe = Pipeline(steps=[('preprocessor', preprocessor), ('regressor', Ridge(alpha=1.0))])
    ml_pipe.fit(X_ridge, y_ridge)
    print("✅ Ridge Regression model training complete!")

    # 릿지 회귀 모델 학습 (리뷰수 예측용)
    X_count = df_products[['discounted_price', 'category_cleaned']]
    y_count = df_products['rating_count']

    count_preprocessor = ColumnTransformer(
        transformers=[
            ('num', StandardScaler(), ['discounted_price']),
            ('cat', OneHotEncoder(handle_unknown='ignore'), ['category_cleaned'])
        ]
    )

    count_pipe = Pipeline(steps=[
        ('preprocessor', count_preprocessor),
        ('regressor', Ridge(alpha=1.0))
    ])
    count_pipe.fit(X_count, y_count)
    print("✅ Count Regression model training complete!")


    # TF-IDF 모델 학습 (유사도 분석용)
    tfidf_vectorizer = TfidfVectorizer(stop_words='english', max_features=5000)
    tfidf_matrix = tfidf_vectorizer.fit_transform(df_products['combined_text'])
    print("✅ TF-IDF model training complete!")
    print(f"📈 Total {len(df_products)} unique products and {len(df_reviews)} individual reviews loaded.")
    print(f"⭐ Rating range found in data: {df_products['rating'].min()} ~ {df_products['rating'].max()}")


# --- 서버 시작 시 실행될 로직 ---
@app.on_event("startup")
def startup_event():
    try:
        load_data_and_train_models()
    except Exception as e:
        print(f"🚨 서버 시작 중 오류 발생: {e}")


# --- API 엔드포인트 정의 ---
@app.get("/")
def read_root():
    return {"message": "Rmazon predictor and similarity API is running!"}

@app.post("/upload-csv")
async def upload_csv(file: UploadFile = File(...)):
    # 임시 파일로 저장
    temp_file_path = f"temp_{file.filename}"
    with open(temp_file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # 새 파일 검증
    try:
        df_new = pd.read_csv(temp_file_path)
        required_columns = ['product_id', 'product_name', 'review_title', 'review_content', 'discounted_price', 'rating_count', 'category_cleaned', 'rating']
        missing = [col for col in required_columns if col not in df_new.columns]
        if missing:
            raise HTTPException(status_code=400, detail=f"필수 컬럼 누락: {', '.join(missing)}")
        
        # 검증 통과 시, 기존 파일 덮어쓰기
        shutil.move(temp_file_path, DATA_FILE_PATH)
        
        # 데이터와 모델 다시 로드
        load_data_and_train_models()
        
        return {"message": "파일이 성공적으로 업로드 및 처리되었습니다.", "rows": len(df_new)}
    
    except Exception as e:
        os.remove(temp_file_path) # 실패 시 임시 파일 제거
        raise HTTPException(status_code=500, detail=f"파일 처리 중 오류 발생: {e}")
    finally:
        await file.close()

@app.get("/categories", response_model=List[str])
def get_categories():
    """사용 가능한 모든 카테고리 목록을 반환합니다."""
    if df_products.empty:
        return []
    return sorted(df_products['category_cleaned'].unique().tolist())

@app.get("/products", response_model=List[Product])
def get_products(category: Optional[str] = Query(None)):
    """
    상품 목록을 반환합니다.
    - category 쿼리 파라미터가 있으면 해당 카테고리의 상품만 필터링합니다.
    """
    if df_products.empty:
        return []
    
    if category:
        filtered_df = df_products[df_products['category_cleaned'] == category]
        return filtered_df[['product_id', 'product_name']].to_dict('records')
    
    # 카테고리가 없으면 전체 목록 반환 (관리용 또는 다른 용도로 유지)
    return df_products[['product_id', 'product_name']].to_dict('records')

# --- 고급 리뷰 분석 로직 (서버 사이드로 이동) ---
def advanced_review_analysis(reviews: List[str]) -> Dict:
    # 이 부분은 이전에 프론트엔드에 있던 로직을 가져온 것입니다.
    # 실제로는 더 정교한 NLP 라이브러리(spaCy, NLTK 등)를 사용해야 하지만,
    # 기존 기능 복원을 위해 동일한 로직을 사용합니다.
    
    # ... (여기에 감성분석, 키워드 추출 등 기존 로직 구현) ...
    # 간단한 구현 예시:
    positive_words = ['good', 'great', 'excellent', 'love', 'best']
    negative_words = ['bad', 'poor', 'terrible', 'hate', 'worst']
    
    sentiments = {'positive': 0, 'neutral': 0, 'negative': 0}
    all_words = []
    
    for review in reviews:
        review_lower = review.lower()
        pos_count = sum(1 for word in positive_words if word in review_lower)
        neg_count = sum(1 for word in negative_words if word in review_lower)
        
        if pos_count > neg_count:
            sentiments['positive'] += 1
        elif neg_count > pos_count:
            sentiments['negative'] += 1
        else:
            sentiments['neutral'] += 1
        
        all_words.extend(review_lower.split())

    # 전체 감성
    overall = max(sentiments, key=sentiments.get)

    # 키워드 (간단한 빈도수 기반)
    from collections import Counter
    # 불용어 리스트 확장
    stop_words_list = list(text.ENGLISH_STOP_WORDS) + ['product', 'amazon', 'use', 'get', 'it', 'i']
    
    keywords_with_counts = [
        (word, count) for word, count in Counter(all_words).most_common(20) 
        if word.isalpha() and len(word) > 2 and word not in stop_words_list
    ]

    return {
        "overall_sentiment": overall,
        "sentiment_distribution": sentiments,
        "top_keywords": [{"word": w, "count": c} for w, c in keywords_with_counts[:5]], # 상위 5개만 선택
        "negative_concerns": [r for r in reviews if any(w in r.lower() for w in negative_words)][:2],
        "summary": f"전체적으로 {overall}적인 평가가 많습니다. 주요 키워드는 {', '.join([k[0] for k in keywords_with_counts[:5]])} 등입니다.",
        "review_count": len(reviews)
    }

@app.get("/category-price-range", response_model=PriceRangeResponse)
def get_category_price_range(category: str = Query(...)):
    """특정 카테고리의 최소 및 최대 가격을 반환합니다."""
    if df_products.empty:
        raise HTTPException(status_code=503, detail="서버 데이터가 준비되지 않았습니다.")
    
    filtered_df = df_products[df_products['category_cleaned'] == category]
    
    if filtered_df.empty:
        # 해당 카테고리에 상품이 없으면 전체 데이터의 가격 범위를 기본값으로 제공
        min_price = df_products['discounted_price'].min()
        max_price = df_products['discounted_price'].max()
    else:
        min_price = filtered_df['discounted_price'].min()
        max_price = filtered_df['discounted_price'].max()

    return {"min_price": min_price, "max_price": max_price}

@app.get("/category-stats", response_model=CategoryStatsResponse)
def get_category_stats(category: str = Query(..., description="통계 정보를 조회할 카테고리")):
    """선택한 카테고리의 가격, 리뷰 수, 별점에 대한 통계 정보를 반환합니다."""
    if df_products.empty:
        raise HTTPException(status_code=503, detail="서버 데이터가 준비되지 않았습니다.")

    filtered_df = df_products[df_products['category_cleaned'] == category]
    if filtered_df.empty:
        raise HTTPException(status_code=404, detail=f"'{category}' 카테고리에 대한 데이터가 없습니다.")

    def create_histogram(data: pd.Series, bins=10):
        if data.empty or data.nunique() < 2: # 데이터가 없거나 모두 같은 값이면 히스토그램 생성 불가
            return [{"name": "N/A", "count": len(data)}]
        
        counts, bin_edges = np.histogram(data.dropna(), bins=bins)
        dist_data = []
        for i in range(len(counts)):
            start = bin_edges[i]
            end = bin_edges[i+1]
            # 정수는 깔끔하게, 실수는 소수점 1자리까지 표기
            label = f"{int(start):,}-{int(end):,}" if start.is_integer() and end.is_integer() else f"{start:,.1f}-{end:,.1f}"
            dist_data.append({"name": label, "count": int(counts[i])})
        return dist_data

    return {
        "min_price": filtered_df['discounted_price'].min(),
        "max_price": filtered_df['discounted_price'].max(),
        "min_review_count": filtered_df['rating_count'].min(),
        "max_review_count": filtered_df['rating_count'].max(),
        "price_distribution": create_histogram(filtered_df['discounted_price']),
        "review_count_distribution": create_histogram(filtered_df['rating_count']),
        "rating_distribution": create_histogram(filtered_df['rating'], bins=8) # 1~5점 별점을 좀 더 세분화
    }

@app.post("/search-similarity", response_model=List[SimilarityResult])
def search_similarity(request: SimilarityRequest):
    """
    입력된 상품 정보와 가장 유사한 상품 목록을 반환합니다.
    유사도는 텍스트(TF-IDF)와 가격을 종합하여 계산됩니다.
    각 유사 상품에 대해 개별적인 리뷰 분석을 수행합니다.
    """
    if df_products.empty or tfidf_matrix is None:
        raise HTTPException(status_code=503, detail="서버가 준비되지 않았거나 데이터가 없습니다.")
    if not request.description.strip() or not request.category:
        raise HTTPException(status_code=400, detail="상품 설명과 카테고리를 모두 입력해주세요.")

    # 1. 텍스트 유사도 계산 (TF-IDF)
    input_vector = tfidf_vectorizer.transform([request.description])
    text_similarities = cosine_similarity(input_vector, tfidf_matrix).flatten()

    # 2. 카테고리가 일치하는 상품만 필터링
    category_mask = df_products['category_cleaned'] == request.category
    
    # 3. 종합 점수 계산
    # 가격 유사도: 요청 가격과의 차이가 적을수록 높음 (정규화)
    price_diff = np.abs(df_products['discounted_price'] - request.price)
    # 0으로 나누는 것을 방지하기 위해 아주 작은 값(epsilon)을 더함
    price_similarity = 1 - (price_diff / (price_diff.max() + 1e-6))
    
    # 종합 점수 = 텍스트 유사도 * 0.7 + 가격 유사도 * 0.3
    combined_scores = (text_similarities * 0.7) + (price_similarity * 0.3)
    
    # 카테고리 마스크 적용
    combined_scores[~category_mask] = 0

    # 4. 상위 5개 상품 선정
    top_n = 5
    # 점수가 0인 경우는 제외하고, 상위 N개를 찾음
    valid_scores_indices = np.where(combined_scores > 0)[0]
    if len(valid_scores_indices) == 0:
        return []
    
    top_indices = valid_scores_indices[np.argsort(combined_scores[valid_scores_indices])[-top_n:]][::-1]

    # 5. 최종 결과 생성 (상품별 개별 분석)
    results = []
    for idx in top_indices:
        product_row = df_products.iloc[idx]
        product_id = product_row['product_id']
        
        # 상품별 개별 리뷰 추출
        product_reviews = df_reviews[df_reviews['product_id'] == product_id]['review_text'].tolist()
        
        # 상품별 리뷰 분석 수행
        if not product_reviews:
            # 리뷰가 없는 경우 기본값 설정
            review_analysis_result = {
                'overall_sentiment': 'neutral',
                'sentiment_distribution': {'positive': 0, 'neutral': 0, 'negative': 0},
                'top_keywords': [], 'negative_concerns': [],
                'summary': '리뷰 데이터가 부족하여 분석할 수 없습니다.',
                'review_count': 0
            }
        else:
            review_analysis_result = advanced_review_analysis(product_reviews)
            
        results.append(SimilarityResult(
            product_id=product_id,
            product_name=product_row['product_name'],
            similarity=combined_scores[idx],
            discounted_price=product_row['discounted_price'],
            rating=product_row['rating'],
            rating_count=product_row['rating_count'],
            review_analysis=review_analysis_result
        ))
        
    return results

@app.post("/predict", response_model=PredictionResponse)
def predict_star_rating(request: PredictionRequest):
    if ml_pipe is None or df_products.empty:
        raise HTTPException(status_code=503, detail="예측 모델이 준비되지 않았습니다.")
    
    # 1차 입력: price + category -> 리뷰 수 예측
    input_for_count = pd.DataFrame([{
        'discounted_price': request.price,
        'category_cleaned': request.category
    }])
    predicted_review_count = max(0, int(count_pipe.predict(input_for_count)[0]))
    
    # 2차 입력: price + 예측된 리뷰 수 + category -> 별점 예측
    input_for_star = pd.DataFrame([{
        'discounted_price': request.price,
        'rating_count': predicted_review_count,
        'category_cleaned': request.category
    }])
    predicted_star = ml_pipe.predict(input_for_star)[0]
    # 모델의 예측 결과가 현실적인 별점 범위(0~5)를 벗어나지 않도록 보정
    clamped_star = max(0.0, min(5.0, predicted_star))

    # 2차 입력: price + 예측된 별점 +
    # --- 백분위 계산 로직 ---
    filtered_df = df_products[df_products['category_cleaned'] == request.category]
    
    def calculate_percentile(series: pd.Series, score: float) -> float:
        if series.empty: return 50.0  # 데이터가 없으면 중간값으로 처리
        return (series < score).sum() / len(series) * 100

    price_percentile = calculate_percentile(filtered_df['discounted_price'], request.price)
    review_count_percentile = calculate_percentile(filtered_df['rating_count'], predicted_review_count)
    rating_percentile = calculate_percentile(filtered_df['rating'], clamped_star)
    
    return {
        "predicted_star": round(clamped_star, 2),
        "predicted_review_count": predicted_review_count,
        "price_percentile": round(price_percentile, 1),
        "review_count_percentile": round(review_count_percentile, 1),
        "rating_percentile": round(rating_percentile, 1),
    }