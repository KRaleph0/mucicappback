import os
import requests
import base64
import oracledb
import random
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, g
from flask_cors import CORS

# --- 1. 설정 (환경 변수 사용 및 공식 URL 적용) ---
# [보안 수정] 기본값(하드코딩된 키)을 제거했습니다. 반드시 docker-compose.yml에서 주입해야 합니다.
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")

# [필수 수정] 401 오류 해결을 위해 공식 Spotify API 주소로 변경했습니다.
SPOTIFY_auth_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

# API 키가 없으면 서버 시작 시 경고를 띄우거나 에러를 냅니다.
if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
    print("🚨 [경고] SPOTIFY_CLIENT_ID 또는 SECRET이 설정되지 않았습니다! 인증에 실패할 수 있습니다.")

# 다른 키들도 환경변수로 빼는 것을 권장하지만, 일단 기존 유지 (필요 시 os.getenv로 변경하세요)
KOBIS_API_KEY = os.getenv("KOBIS_API_KEY", "8a96e3a327421cc09bab673061f9aa97")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "5b4d4311c310d9b732b954cc0c9628db")

# Oracle DB 설정
DB_USER = os.getenv("DB_USER", "admin")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
DB_DSN = os.getenv("DB_DSN", "ordb.mirinea.org:1521/XEPDB1")

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

# --- 2. Spotify 인증 ---
def get_spotify_headers():
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise Exception("Spotify API Key가 설정되지 않음")
        
    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    
    # 공식 인증 URL 사용
    res = requests.post(SPOTIFY_auth_URL, 
                        headers={'Authorization': f'Basic {b64_auth}', 'Content-Type': 'application/x-www-form-urlencoded'}, 
                        data={'grant_type': 'client_credentials'})
    
    if res.status_code != 200:
        print(f"[Spotify 인증 실패] {res.text}")
        raise Exception(f"Spotify Auth Failed: {res.status_code}")
        
    token = res.json().get('access_token')
    return {'Authorization': f'Bearer {token}'}

# --- 3. [핵심] 영화 데이터 수집 및 DB 저장 ---
def update_box_office_data():
    """KOBIS -> TMDB -> Spotify -> Oracle DB 저장"""
    print("[Batch] 박스오피스 업데이트 시작...")
    try:
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
                    if m_data.get('original_title'):
                        search_query += f" {m_data['original_title']}"
            except: pass

            # 3. Spotify OST 검색
            search_query += " ost"
            # 공식 API 사용
            sp_res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, 
                                params={"q": search_query, "type": "track", "limit": 1}).json()
            
            tracks = sp_res.get('tracks', {}).get('items', [])
            if not tracks:
                print(f"    -> Spotify 결과 없음: {title}")
                continue
                
            track = tracks[0]
            track_id = track['id']

            # 4. DB 저장
            db_check_or_create_track(track_id) 

            # 5. 영화 정보 저장
            try:
                cursor.execute("""
                    MERGE INTO MOVIES m USING (SELECT :1 AS mid FROM dual) d
                    ON (m.movie_id = d.mid)
                    WHEN MATCHED THEN UPDATE SET rank = :2, poster_url = :3
                    WHEN NOT MATCHED THEN INSERT (movie_id, title, rank, poster_url) VALUES (:1, :4, :2, :3)
                """, [title, rank, poster_url, title])

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
        
    except Exception as e:
        print(f"[Batch 오류] {e}")
        return f"업데이트 실패: {e}"

# --- 4. 트랙 저장 및 자동 태깅 ---
def db_check_or_create_track(track_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT track_id FROM TRACKS WHERE track_id = :1", [track_id])
    if cursor.fetchone(): return

    headers = get_spotify_headers()
    # 공식 API 사용
    track_data = requests.get(f"{SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers).json()
    feats = requests.get(f"{SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers).json()

    # (여기 INSERT 로직은 DB 스키마에 맞춰 유지)
    
    tags = []
    if feats:
        energy = feats.get('energy', 0)
        valence = feats.get('valence', 0)
        
        if energy > 0.7: tags.append('tag:Exciting')
        if energy < 0.4: tags.append('tag:Rest')
        if valence < 0.3: tags.append('tag:Sentimental')
        if 0.4 <= valence <= 0.7: tags.append('tag:Pop')

    for tag_id in tags:
        try:
            cursor.execute("INSERT INTO TRACK_TAGS (track_id, tag_id) VALUES (:1, :2)", [track_id, tag_id])
        except: pass
    
    conn.commit()

# --- 5. API 라우트 ---

@app.route('/api/spotify-token', methods=['GET'])
def api_get_token():
    """프론트엔드에 Spotify Access Token 발급"""
    try:
        if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
            return jsonify({"error": "Server API Key not configured"}), 500

        auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()
        
        # 공식 인증 URL
        res = requests.post(SPOTIFY_auth_URL, 
                          headers={'Authorization': f'Basic {b64_auth}', 'Content-Type': 'application/x-www-form-urlencoded'}, 
                          data={'grant_type': 'client_credentials'})
        
        if res.status_code == 200:
            token = res.json().get('access_token')
            return jsonify({"access_token": token})
        else:
            print(f"[Spotify Error] {res.text}")
            return jsonify({"error": "Spotify Auth Failed", "details": res.text}), res.status_code

    except Exception as e:
        print(f"[Server Error] {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/search', methods=['GET'])
def api_search():
    """음악 검색 API (프록시 역할)"""
    query = request.args.get('q', '')
    search_type = request.args.get('type', 'track')
    
    if not query:
        return jsonify({"error": "검색어를 입력해주세요."}), 400

    try:
        headers = get_spotify_headers()
        params = {
            "q": query,
            "type": search_type,
            "limit": 20,
            "market": "KR"
        }
        # 공식 API 사용
        response = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
        
        if response.status_code != 200:
            return jsonify(response.json()), response.status_code
            
        return jsonify(response.json())
        
    except Exception as e:
        print(f"[검색 오류] {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/update-movies', methods=['POST'])
def api_update_movies():
    msg = update_box_office_data()
    return jsonify({"message": msg})

@app.route('/api/recommend/weather', methods=['GET'])
def api_recommend_weather():
    condition = request.args.get('condition', 'Clear')
    tag_map = {'Clear': 'tag:Clear', 'Rain': 'tag:Rain', 'Snow': 'tag:Snow', 'Clouds': 'tag:Cloudy'}
    target_tag = tag_map.get(condition, 'tag:Clear')

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
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
    except Exception as e:
        print(f"[DB Error] {e}")
        return jsonify([])

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)