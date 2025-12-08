import os
import requests
import base64
import oracledb
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, g, Response
from flask_cors import CORS

# --- 1. 설정 ---
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
KOBIS_API_KEY = os.getenv("KOBIS_API_KEY")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")

SPOTIFY_auth_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

KOBIS_BOXOFFICE_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/boxoffice/searchDailyBoxOfficeList.json"
KOBIS_MOVIE_LIST_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/movie/searchMovieList.json"

if not all([SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, KOBIS_API_KEY, TMDB_API_KEY]):
    print("🚨 [CRITICAL] API 키 설정 누락! docker-compose.yml을 확인하세요.")

DB_USER = os.getenv("DB_USER", "admin")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
DB_DSN = os.getenv("DB_DSN", "ordb.mirinea.org:1521/XEPDB1")

app = Flask(__name__)
CORS(app)

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
        raise Exception(f"Spotify Auth Failed: {res.status_code}")
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
            print(f"    🔍 장르 발견: {movie_name} -> {genre_str}")
            return genre_str.split(',') if genre_str else []
        return []
    except Exception:
        return []

# --- 4. 트랙 저장 및 장르 매핑 (수정됨: 딕셔너리 바인딩 사용) ---
def db_save_track_with_genre_tags(track_id, genres, cursor, headers):
    cursor.execute("SELECT track_id FROM TRACKS WHERE track_id = :tid", {'tid': track_id})
    if not cursor.fetchone():
        try:
            track_data = requests.get(f"{SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers).json()
            track_title = track_data.get('name', 'Unknown')
            preview_url = track_data.get('preview_url', '')
            artist_name = track_data['artists'][0]['name'] if track_data.get('artists') else 'Unknown'
            album_id = track_data.get('album', {}).get('id')
            album_cover = track_data.get('album', {}).get('images', [{}])[0].get('url', '')

            # [수정] 앨범 저장 (딕셔너리 사용)
            if album_id:
                cursor.execute("""
                    MERGE INTO ALBUMS USING dual ON (album_id = :aid) 
                    WHEN NOT MATCHED THEN INSERT (album_id, album_cover_url) VALUES (:aid, :cover)
                """, {'aid': album_id, 'cover': album_cover})
            
            # [수정] 트랙 저장 (딕셔너리 사용)
            cursor.execute("""
                INSERT INTO TRACKS (track_id, track_title, preview_url, artist_name, album_id)
                VALUES (:tid, :title, :preview, :artist, :aid)
            """, {'tid': track_id, 'title': track_title, 'preview': preview_url, 'artist': artist_name, 'aid': album_id})
            
        except Exception as e:
            print(f"    ⚠️ 트랙 정보 저장 실패 ({track_id}): {e}")
            return

    genre_map = {
        "액션": "tag:Action", "SF": "tag:SF", "코미디": "tag:Exciting",
        "드라마": "tag:Sentimental", "멜로": "tag:Romance", "로맨스": "tag:Romance",
        "공포": "tag:Tension", "호러": "tag:Tension", "스릴러": "tag:Tension",
        "범죄": "tag:Tension", "애니메이션": "tag:Animation",
        "가족": "tag:Rest", "뮤지컬": "tag:Pop"
    }
    
    tags_to_add = set(["tag:MovieOST"])
    for g in genres:
        for key, tag in genre_map.items():
            if key in g: tags_to_add.add(tag)
    
    # [수정] 태그 저장 (딕셔너리 사용)
    for tag_id in tags_to_add:
        try:
            cursor.execute("""
                MERGE INTO TRACK_TAGS USING dual ON (track_id = :tid AND tag_id = :tag) 
                WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (:tid, :tag)
            """, {'tid': track_id, 'tag': tag_id})
        except: pass
    
    cursor.connection.commit()

# --- 5. 데이터 업데이트 (수정됨: 딕셔너리 바인딩 사용) ---
def update_box_office_data():
    print("[Batch] 박스오피스 업데이트 시작...")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        headers = get_spotify_headers()

        target_dt = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        res = requests.get(KOBIS_BOXOFFICE_URL, params={"key": KOBIS_API_KEY, "targetDt": target_dt, "itemPerPage": "10"}).json()
        movie_list = res.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])

        if not movie_list:
            return "박스오피스 데이터 없음"

        for movie in movie_list:
            rank = int(movie['rank'])
            title = movie['movieNm']
            print(f"\n[Rank {rank}] {title} 처리 중...")

            genres = get_movie_genre(title)
            poster_url = None
            try:
                tmdb_res = requests.get("https://api.themoviedb.org/3/search/movie", 
                                      params={"api_key": TMDB_API_KEY, "query": title, "language": "ko-KR"}).json()
                if tmdb_res.get('results'):
                    m_data = tmdb_res['results'][0]
                    if m_data.get('poster_path'):
                        poster_url = f"https://image.tmdb.org/t/p/w500{m_data['poster_path']}"
            except: pass

            search_query = f"{title} ost"
            params = {"q": search_query, "type": "track", "limit": 1, "market": "KR"}
            track_id = None
            try:
                sp_res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params).json()
                tracks = sp_res.get('tracks', {}).get('items', [])
                if tracks:
                    track_id = tracks[0]['id']
                    db_save_track_with_genre_tags(track_id, genres, cursor, headers)
            except Exception as e:
                print(f"    ⚠️ Spotify 검색 오류: {e}")

            # [수정] 영화 정보 저장 (딕셔너리 바인딩으로 에러 해결)
            try:
                # 영화 정보 저장
                cursor.execute("""
                    MERGE INTO MOVIES m USING (SELECT :mid AS mid FROM dual) d
                    ON (m.movie_id = d.mid)
                    WHEN MATCHED THEN UPDATE SET rank = :rank, poster_url = :poster
                    WHEN NOT MATCHED THEN INSERT (movie_id, title, rank, poster_url) VALUES (:mid, :title, :rank, :poster)
                """, {'mid': title, 'title': title, 'rank': rank, 'poster': poster_url})

                # 영화-OST 연결 저장
                if track_id:
                    cursor.execute("""
                        MERGE INTO MOVIE_OSTS mo USING (SELECT :mid AS mid, :tid AS tid FROM dual) d
                        ON (mo.movie_id = d.mid AND mo.track_id = d.tid)
                        WHEN NOT MATCHED THEN INSERT (movie_id, track_id) VALUES (:mid, :tid)
                    """, {'mid': title, 'tid': track_id})
                
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"    ❌ DB 저장 실패: {e}")

        print("\n[Batch] 업데이트 완료")
        return f"{len(movie_list)}개 영화 업데이트 완료"
    except Exception as e:
        print(f"[Batch 치명적 오류] {e}")
        return f"업데이트 실패: {e}"

# --- 6. API 라우트 (기존 유지) ---
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

@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_box_office_ttl():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 음악 정보가 없어도 영화 정보는 나오도록 LEFT JOIN
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
            m_uri = base64.urlsafe_b64encode(mid.encode()).decode().rstrip("=")
            mposter = mposter or "img/playlist-placeholder.png"
            ttitle = ttitle or "OST 정보 없음"
            artist = artist or "-"
            cover = cover or "img/playlist-placeholder.png"
            preview = preview or ""

            ttl += f"""
<https://knowledgemap.kr/komc/resource/movie/{m_uri}> a schema:Movie ;
    schema:name "{mtitle}" ; schema:image "{mposter}" ; komc:rank {rank} .
<https://knowledgemap.kr/komc/resource/track/{m_uri}_ost> a schema:MusicRecording ;
    schema:name "{ttitle}" ; schema:byArtist "{artist}" ; schema:image "{cover}" ;
    schema:audio "{preview}" ;
    komc:featuredIn <https://knowledgemap.kr/komc/resource/movie/{m_uri}> .
"""
        return Response(ttl, mimetype='text/turtle')
    except Exception as e:
        return f"# Error: {e}", 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)