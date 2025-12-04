import os
import requests
import base64
import oracledb
import random
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, g
from flask_cors import CORS

# --- 1. 설정 (API 키 및 DB) ---
# [기존 설정 유지]
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "f31f9f9e292a47f6b687645f25cfdb19")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "7b287aa77a51486ba95544983f5d7a63")
KOBIS_API_KEY = "8a96e3a327421cc09bab673061f9aa97" # moviesound.py에서 가져옴
TMDB_API_KEY = "5b4d4311c310d9b732b954cc0c9628db"   # moviesound.py에서 가져옴

SPOTIFY_auth_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

# Oracle DB 설정
DB_USER = os.getenv("DB_USER", "admin")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
DB_DSN = "ordb.mirinea.org:1521/XEPDB1" # 예시 DSN

app = Flask(__name__)
CORS(app)

# DB 연결 풀 생성
try:
    db_pool = oracledb.create_pool(user=DB_USER, password=DB_PASSWORD, dsn=DB_DSN, min=1, max=5)
    print("[DB] Oracle Pool 생성 완료.")
except Exception as e:
    print(f"[DB 오류] {e}")
    db_pool = None

def get_db_connection():
    if not db_pool: raise Exception("DB 풀 없음")
    if 'db' not in g: g.db = db_pool.acquire()
    return g.db

@app.teardown_appcontext
def close_db(e):
    db = g.pop('db', None)
    if db: db.release()

# --- 2. Spotify 인증 (기존 유지) ---
def get_spotify_headers():
    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    res = requests.post(SPOTIFY_auth_URL, headers={'Authorization': f'Basic {b64_auth}'}, data={'grant_type': 'client_credentials'})
    token = res.json().get('access_token')
    return {'Authorization': f'Bearer {token}'}

# --- 3. [핵심] 영화 데이터 수집 및 DB 저장 (moviesound.py 통합) ---
def update_box_office_data():
    """KOBIS -> TMDB -> Spotify -> Oracle DB 저장"""
    print("[Batch] 박스오피스 업데이트 시작...")
    conn = get_db_connection()
    cursor = conn.cursor()
    headers = get_spotify_headers()

    # 1. KOBIS 박스오피스 조회
    target_dt = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    kobis_url = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/boxoffice/searchDailyBoxOfficeList.json"
    res = requests.get(kobis_url, params={"key": KOBIS_API_KEY, "targetDt": target_dt, "itemPerPage": "10"}).json()
    movie_list = res.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])

    for movie in movie_list:
        rank = movie['rank']
        title = movie['movieNm']
        print(f"  [{rank}위] {title} 처리 중...")

        # 2. TMDB 포스터 및 원제 검색
        poster_url = None
        search_query = title
        try:
            tmdb_res = requests.get("https://api.themoviedb.org/3/search/movie", 
                                  params={"api_key": TMDB_API_KEY, "query": title, "language": "ko-KR"}).json()
            if tmdb_res.get('results'):
                m_data = tmdb_res['results'][0]
                if m_data.get('poster_path'):
                    poster_url = f"https://image.tmdb.org/t/p/w500{m_data['poster_path']}"
                # 원제(original_title)를 검색어로 추가
                if m_data.get('original_title'):
                    search_query += f" {m_data['original_title']}"
        except: pass

        # 3. Spotify OST 검색
        search_query += " ost"
        sp_res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, 
                            params={"q": search_query, "type": "track", "limit": 1}).json()
        
        tracks = sp_res.get('tracks', {}).get('items', [])
        if not tracks:
            print(f"    -> Spotify 결과 없음: {title}")
            continue
            
        track = tracks[0]
        track_id = track['id']

        # 4. DB 저장: 트랙 정보 (기존 함수 재활용 가능, 여기선 직접 호출)
        # 트랙이 DB에 없으면 생성 (자동 태깅 포함)
        db_check_or_create_track(track_id) 

        # 5. DB 저장: 영화 정보 및 연결
        try:
            # 영화 정보 MERGE
            cursor.execute("""
                MERGE INTO MOVIES m USING (SELECT :1 AS mid FROM dual) d
                ON (m.movie_id = d.mid)
                WHEN MATCHED THEN UPDATE SET rank = :2, poster_url = :3
                WHEN NOT MATCHED THEN INSERT (movie_id, title, rank, poster_url) VALUES (:1, :4, :2, :3)
            """, [title, rank, poster_url, title])

            # 영화-OST 연결
            cursor.execute("""
                MERGE INTO MOVIE_OSTS mo USING (SELECT :1 AS mid, :2 AS tid FROM dual) d
                ON (mo.movie_id = d.mid AND mo.track_id = d.tid)
                WHEN NOT MATCHED THEN INSERT (movie_id, track_id) VALUES (:1, :2)
            """, [title, track_id])
            
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"    -> DB 저장 실패: {e}")

    print("[Batch] 업데이트 완료")
    return f"{len(movie_list)}개 영화 업데이트 완료"

# --- 4. 트랙 저장 및 자동 태깅 (Auto-Tagging) ---
def db_check_or_create_track(track_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # 이미 있는지 확인
    cursor.execute("SELECT track_id FROM TRACKS WHERE track_id = :1", [track_id])
    if cursor.fetchone(): return

    # Spotify 상세 정보 가져오기
    headers = get_spotify_headers()
    track_data = requests.get(f"{SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers).json()
    # [중요] 오디오 특징 가져오기 (BPM, Energy, Valence 등)
    feats = requests.get(f"{SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers).json()

    # DB INSERT (TRACKS 테이블 - 생략된 필드는 기존 코드 참고)
    # ... (기존 backend_api.py의 INSERT 로직 사용) ...
    # 여기서는 예시로 태그 로직만 보여드립니다.
    
    # [자동 태깅 로직]
    tags = []
    if feats:
        energy = feats.get('energy', 0)
        valence = feats.get('valence', 0)
        
        if energy > 0.7: tags.append('tag:Exciting')      # 신나는
        if energy < 0.4: tags.append('tag:Rest')          # 휴식
        if valence < 0.3: tags.append('tag:Sentimental')  # 센치한/우울한
        if 0.4 <= valence <= 0.7: tags.append('tag:Pop')  # 팝 느낌

    # 태그 저장
    for tag_id in tags:
        try:
            cursor.execute("INSERT INTO TRACK_TAGS (track_id, tag_id) VALUES (:1, :2)", [track_id, tag_id])
        except: pass # 중복 무시
    
    conn.commit()

# --- 5. API 라우트 ---

@app.route('/api/admin/update-movies', methods=['POST'])
def api_update_movies():
    """(관리자용) 박스오피스 강제 업데이트"""
    msg = update_box_office_data()
    return jsonify({"message": msg})

@app.route('/api/recommend/weather', methods=['GET'])
def api_recommend_weather():
    """날씨 기반 추천 (DB 태그 조회)"""
    condition = request.args.get('condition', 'Clear') # Clear, Rain, Snow
    tag_map = {'Clear': 'tag:Clear', 'Rain': 'tag:Rain', 'Snow': 'tag:Snow', 'Clouds': 'tag:Cloudy'}
    target_tag = tag_map.get(condition, 'tag:Clear')

    conn = get_db_connection()
    cursor = conn.cursor()
    # 태그가 일치하는 노래 + 영화 정보가 있다면 영화 정보까지 조인
    cursor.execute("""
        SELECT t.track_title, t.preview_url, a.album_cover_url, m.title as movie_title
        FROM TRACKS t
        JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
        JOIN ALBUMS a ON t.album_id = a.album_id
        LEFT JOIN MOVIE_OSTS mo ON t.track_id = mo.track_id
        LEFT JOIN MOVIES m ON mo.movie_id = m.movie_id
        WHERE tt.tag_id = :1
    """, [target_tag])
    
    results = []
    for row in cursor.fetchall():
        results.append({
            "title": row[0], "preview": row[1], "cover": row[2], "movie": row[3]
        })
    return jsonify(results)

if __name__ == '__main__':
    app.run(debug=True, port=5000)