import os
import requests
import base64
import oracledb
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, g, Response
from flask_cors import CORS

# --- 1. 설정 (환경 변수 필수) ---
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
KOBIS_API_KEY = os.getenv("KOBIS_API_KEY")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")

# Spotify 공식 API 주소 (프록시 대신 공식 주소 사용 권장)
# 만약 이 주소로 안 되면 https://accounts.spotify.com/api/token 등으로 변경 필요할 수 있음
SPOTIFY_auth_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

# KOBIS URL
KOBIS_BOXOFFICE_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/boxoffice/searchDailyBoxOfficeList.json"
KOBIS_MOVIE_LIST_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/movie/searchMovieList.json"

# 키 확인
if not all([SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, KOBIS_API_KEY, TMDB_API_KEY]):
    print("🚨 [CRITICAL] 주요 API 키(Spotify, KOBIS, TMDB) 설정이 누락되었습니다! docker-compose.yml을 확인하세요.")

# DB 설정
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
    if db: db.close() # [수정] release() -> close() (최신 oracledb 문법)

# --- 2. Spotify 인증 ---
def get_spotify_headers():
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise Exception("Spotify API Key가 설정되지 않음")

    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    
    headers = {
        'Authorization': f'Basic {b64_auth}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {'grant_type': 'client_credentials'}
    
    res = requests.post(SPOTIFY_auth_URL, headers=headers, data=data)
    if res.status_code != 200:
        raise Exception(f"Spotify Auth Failed: {res.status_code} {res.text}")
        
    token = res.json().get('access_token')
    return {'Authorization': f'Bearer {token}'}

# --- 3. 영화 장르 조회 (KOBIS) ---
def get_movie_genre(movie_name):
    params = {'key': KOBIS_API_KEY, 'movieNm': movie_name}
    try:
        response = requests.get(KOBIS_MOVIE_LIST_URL, params=params)
        data = response.json()
        movie_list = data.get('movieListResult', {}).get('movieList', [])
        if movie_list:
            # 첫 번째 결과의 장르 문자열 반환 (예: "액션,드라마")
            genre_str = movie_list[0].get('genreAlt', '')
            return genre_str.split(',') if genre_str else []
        return []
    except Exception as e:
        print(f"⚠️ 장르 조회 실패 ({movie_name}): {e}")
        return []

# --- 4. 트랙 저장 및 장르 태깅 함수 ---
def db_save_track_with_genre_tags(track_id, genres, cursor, headers):
    # 1. 트랙 기본 정보 저장 (이미 있으면 패스)
    cursor.execute("SELECT track_id FROM TRACKS WHERE track_id = :1", [track_id])
    if not cursor.fetchone():
        try:
            # Spotify 상세 정보 조회
            track_data = requests.get(f"{SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers).json()
            track_title = track_data.get('name', 'Unknown Title')
            preview_url = track_data.get('preview_url', '')
            artist_name = track_data['artists'][0]['name'] if track_data.get('artists') else 'Unknown Artist'
            
            # 앨범 정보 처리
            album_id = track_data.get('album', {}).get('id')
            album_cover = track_data.get('album', {}).get('images', [{}])[0].get('url', '')
            
            # 앨범 테이블 저장
            if album_id:
                cursor.execute("MERGE INTO ALBUMS USING dual ON (album_id = :1) WHEN NOT MATCHED THEN INSERT (album_id, album_cover_url) VALUES (:1, :2)", [album_id, album_cover])
            
            # 트랙 테이블 저장
            cursor.execute("""
                INSERT INTO TRACKS (track_id, track_title, preview_url, artist_name, album_id)
                VALUES (:1, :2, :3, :4, :5)
            """, [track_id, track_title, preview_url, artist_name, album_id])
            
        except Exception as e:
            print(f"⚠️ 트랙 정보 저장 실패 ({track_id}): {e}")
            return # 트랙 저장이 안 되면 태그 저장도 스킵

    # 2. 영화 장르를 음악 태그로 매핑하여 저장
    genre_map = {
        "액션": "tag:Action", "SF": "tag:SF", "코미디": "tag:Exciting",
        "드라마": "tag:Sentimental", "멜로/로맨스": "tag:Romance",
        "공포": "tag:Tension", "스릴러": "tag:Tension", "애니메이션": "tag:Animation"
    }
    
    tags_to_add = ["tag:MovieOST"] # 기본 태그
    for g in genres:
        if g in genre_map:
            tags_to_add.append(genre_map[g])
    
    # DB에 태그 저장
    for tag_id in tags_to_add:
        try:
            cursor.execute("MERGE INTO TRACK_TAGS USING dual ON (track_id = :1 AND tag_id = :2) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (:1, :2)", [track_id, tag_id])
        except: pass

# --- 5. 데이터 업데이트 (배치 작업) ---
def update_box_office_data():
    print("[Batch] 박스오피스 업데이트 시작...")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        headers = get_spotify_headers()

        target_dt = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        res = requests.get(KOBIS_BOXOFFICE_URL, params={"key": KOBIS_API_KEY, "targetDt": target_dt, "itemPerPage": "10"}).json()
        movie_list = res.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])

        for movie in movie_list:
            rank = int(movie['rank'])
            title = movie['movieNm']
            print(f"  [{rank}위] {title} 처리 중...")

            # 영화 장르 조회
            genres = get_movie_genre(title)

            # TMDB 포스터 검색
            poster_url = None
            try:
                tmdb_res = requests.get("https://api.themoviedb.org/3/search/movie", 
                                      params={"api_key": TMDB_API_KEY, "query": title, "language": "ko-KR"}).json()
                if tmdb_res.get('results'):
                    m_data = tmdb_res['results'][0]
                    if m_data.get('poster_path'):
                        poster_url = f"https://image.tmdb.org/t/p/w500{m_data['poster_path']}"
            except: pass

            # Spotify OST 검색
            search_query = f"{title} ost"
            params = {"q": search_query, "type": "track", "limit": 1, "market": "KR"}
            try:
                sp_res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params).json()
                tracks = sp_res.get('tracks', {}).get('items', [])
                
                track_id = None
                if tracks:
                    track = tracks[0]
                    track_id = track['id']
                    # 트랙 저장 및 장르 태그 매핑
                    db_save_track_with_genre_tags(track_id, genres, cursor, headers)
            except Exception as e:
                print(f"    ⚠️ Spotify 검색 실패: {e}")
                track_id = None

            # 영화 정보 저장 및 매핑
            try:
                # 영화 정보 저장
                cursor.execute("""
                    MERGE INTO MOVIES m USING (SELECT :1 AS mid FROM dual) d
                    ON (m.movie_id = d.mid)
                    WHEN MATCHED THEN UPDATE SET rank = :2, poster_url = :3
                    WHEN NOT MATCHED THEN INSERT (movie_id, title, rank, poster_url) VALUES (:1, :4, :2, :3)
                """, [title, rank, poster_url, title])

                # 영화-OST 연결 (OST를 찾은 경우에만)
                if track_id:
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

# --- 6. API 라우트 ---

@app.route('/api/spotify-token', methods=['GET'])
def api_get_token():
    try:
        headers = get_spotify_headers()
        token = headers['Authorization'].split(' ')[1]
        return jsonify({"access_token": token})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/search', methods=['GET'])
def api_search():
    query = request.args.get('q', '')
    search_type = request.args.get('type', 'track')
    if not query: return jsonify({"error": "검색어 필요"}), 400
    try:
        headers = get_spotify_headers()
        params = {"q": query, "type": search_type, "limit": 20, "market": "KR"}
        res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
        return jsonify(res.json()), res.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/admin/update-movies', methods=['POST'])
def api_update_movies():
    msg = update_box_office_data()
    return jsonify({"message": msg})

# [NEW] 실시간 TTL 생성 API (LEFT JOIN 적용됨)
@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_box_office_ttl():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # [수정] 음악 정보가 없어도 영화 정보는 나오도록 LEFT JOIN 사용
        query = """
            SELECT 
                m.movie_id, m.title, m.rank, m.poster_url,
                t.track_title, t.artist_name, t.preview_url, a.album_cover_url
            FROM MOVIES m
            LEFT JOIN MOVIE_OSTS mo ON m.movie_id = mo.movie_id
            LEFT JOIN TRACKS t ON mo.track_id = t.track_id
            LEFT JOIN ALBUMS a ON t.album_id = a.album_id
            WHERE m.rank <= 10
            ORDER BY m.rank ASC
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        
        ttl = """@prefix schema: <http://schema.org/> .
@prefix komc: <https://knowledgemap.kr/komc/def/> .
"""
        for row in rows:
            mid, mtitle, rank, mposter, ttitle, artist, preview, cover = row
            
            # ID 인코딩 (Base64)
            m_uri = base64.urlsafe_b64encode(mid.encode()).decode().rstrip("=")
            
            # None 값 처리
            mposter = mposter or "img/playlist-placeholder.png"
            ttitle = ttitle or "OST 정보 없음"
            artist = artist or "-"
            cover = cover or "img/playlist-placeholder.png"
            preview = preview or ""

            # 영화 데이터
            ttl += f"""
<https://knowledgemap.kr/komc/resource/movie/{m_uri}> a schema:Movie ;
    schema:name "{mtitle}" ;
    schema:image "{mposter}" ;
    komc:rank {rank} .
"""
            # 트랙 데이터 (영화와 연결)
            ttl += f"""
<https://knowledgemap.kr/komc/resource/track/{m_uri}_ost> a schema:MusicRecording ;
    schema:name "{ttitle}" ;
    schema:byArtist "{artist}" ;
    schema:image "{cover}" ;
    schema:audio "{preview}" ;
    komc:featuredIn <https://knowledgemap.kr/komc/resource/movie/{m_uri}> .
"""
        return Response(ttl, mimetype='text/turtle')
    except Exception as e:
        print(f"❌ TTL 생성 오류: {e}")
        return f"# Error: {e}", 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)