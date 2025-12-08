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

# Spotify 공식 API 주소
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

# DB 연결 풀
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
    if db: db.close()

# --- 2. Spotify 인증 ---
def get_spotify_headers():
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

# --- 3. 영화 장르 조회 ---
def get_movie_genre(movie_name):
    params = {'key': KOBIS_API_KEY, 'movieNm': movie_name}
    try:
        response = requests.get(KOBIS_MOVIE_LIST_URL, params=params)
        data = response.json()
        movie_list = data.get('movieListResult', {}).get('movieList', [])
        if movie_list:
            genre_str = movie_list[0].get('genreAlt', '')
            return genre_str.split(',') if genre_str else []
        return []
    except Exception as e:
        print(f"⚠️ 장르 조회 실패 ({movie_name}): {e}")
        return []

# --- 4. 데이터 업데이트 (배치 작업) ---
def update_box_office_data():
    print("[Batch] 박스오피스 업데이트 시작...")
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

        genres = get_movie_genre(title)

        # TMDB 포스터
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

        # Spotify OST
        search_query += " ost"
        params = {"q": search_query, "type": "track", "limit": 1, "market": "KR"}
        sp_res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params).json()
        
        tracks = sp_res.get('tracks', {}).get('items', [])
        if not tracks: continue
            
        track = tracks[0]
        track_id = track['id']

        # DB 저장 (트랙 + 장르 태그)
        db_save_track_with_genre_tags(track_id, genres, cursor, headers)

        # 영화 정보 저장
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

    return f"{len(movie_list)}개 영화 업데이트 완료"

def db_save_track_with_genre_tags(track_id, genres, cursor, headers):
    # 트랙 기본 정보 저장 (존재 여부 확인 후 INSERT)
    cursor.execute("SELECT track_id FROM TRACKS WHERE track_id = :1", [track_id])
    if not cursor.fetchone():
        track_data = requests.get(f"{SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers).json()
        # (INSERT 로직은 기존 스키마에 맞춰 구현 - 생략)
        # 예시: cursor.execute("INSERT INTO TRACKS ...", [...])
        pass

    # 장르 -> 태그 매핑
    genre_map = {
        "액션": "tag:Action", "SF": "tag:SF", "코미디": "tag:Exciting",
        "드라마": "tag:Sentimental", "멜로/로맨스": "tag:Romance",
        "공포": "tag:Tension", "스릴러": "tag:Tension", "애니메이션": "tag:Animation"
    }
    tags = ["tag:MovieOST"]
    for g in genres:
        if g in genre_map: tags.append(genre_map[g])
    
    for tag_id in tags:
        try:
            cursor.execute("MERGE INTO TRACK_TAGS USING dual ON (track_id = :1 AND tag_id = :2) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (:1, :2)", [track_id, tag_id])
        except: pass
    cursor.connection.commit()

# --- 5. API 라우트 ---

@app.route('/api/spotify-token', methods=['GET'])
def api_get_token():
    try:
        headers = get_spotify_headers()
        # 헤더에서 토큰만 추출 ('Bearer ' 제거)
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

# [NEW] 실시간 TTL 생성 API
@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_box_office_ttl():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        query = """
            SELECT m.rank, m.title, m.poster_url, m.movie_id,
                   t.track_id, t.track_title, t.preview_url, a.album_cover_url, t.artist_name
            FROM MOVIES m
            JOIN MOVIE_OSTS mo ON m.movie_id = mo.movie_id
            JOIN TRACKS t ON mo.track_id = t.track_id
            LEFT JOIN ALBUMS a ON t.album_id = a.album_id
            WHERE m.rank <= 10 ORDER BY m.rank ASC
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        
        ttl = """@prefix schema: <http://schema.org/> .
@prefix komc: <https://knowledgemap.kr/komc/def/> .
"""
        for row in rows:
            rank, m_title, poster, m_id, t_id, t_title, preview, cover, artist = row
            m_uri = base64.urlsafe_b64encode(m_id.encode()).decode().rstrip("=")
            poster = poster or "img/playlist-placeholder.png"
            cover = cover or "img/playlist-placeholder.png"
            artist = artist or "Unknown"
            
            ttl += f"""
<https://knowledgemap.kr/komc/resource/movie/{m_uri}> a schema:Movie ;
    schema:name "{m_title}" ; schema:image "{poster}" ; komc:rank {rank} .
<https://knowledgemap.kr/komc/resource/track/{t_id}> a schema:MusicRecording ;
    schema:name "{t_title}" ; schema:byArtist "{artist}" ; schema:image "{cover}" ;
    schema:audio "{preview or ''}" ;
    komc:featuredIn <https://knowledgemap.kr/komc/resource/movie/{m_uri}> .
"""
        return Response(ttl, mimetype='text/turtle')
    except Exception as e:
        return f"# Error: {e}", 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)