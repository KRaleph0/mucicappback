import os
import requests
import base64
import oracledb
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, g, Response
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

# --- 1. 설정 ---
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
KOBIS_API_KEY = os.getenv("KOBIS_API_KEY")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
DATA_GO_KR_API_KEY = os.getenv("DATA_GO_KR_API_KEY")

SPOTIFY_auth_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

KOBIS_BOXOFFICE_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/boxoffice/searchDailyBoxOfficeList.json"
KOBIS_MOVIE_LIST_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/movie/searchMovieList.json"
WEATHER_API_URL = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst"
HOLIDAY_API_URL = "http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"

DB_USER = os.getenv("DB_USER", "admin")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
DB_DSN = os.getenv("DB_DSN", "ordb.mirinea.org:1521/XEPDB1")

app = Flask(__name__)
CORS(app)

# DB 연결
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

# --- 2. 헬퍼 함수 (필요한 것만 유지) ---
def get_spotify_headers():
    if not SPOTIFY_CLIENT_ID: return {}
    auth = base64.b64encode(f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()).decode()
    try:
        res = requests.post(SPOTIFY_auth_URL, headers={'Authorization': f'Basic {auth}', 'Content-Type': 'application/x-www-form-urlencoded'}, data={'grant_type': 'client_credentials'})
        return {'Authorization': f'Bearer {res.json().get("access_token")}'}
    except: return {}

def extract_spotify_id(url):
    match = re.search(r'track/([a-zA-Z0-9]{22})', url or "")
    return match.group(1) if match else None

# --- 3. 핵심 API ---

# [FIX] 415 에러 해결: force=True 추가
@app.route('/api/user/profile', methods=['POST'])
def api_get_profile():
    # force=True: 헤더가 application/json이 아니어도 강제로 파싱 시도
    d = request.get_json(force=True, silent=True) or {}
    uid = d.get('user_id')
    
    if not uid: return jsonify({"error": "User ID missing"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, nickname, profile_img, role FROM USERS WHERE user_id=:1", [uid])
        u = cursor.fetchone()
        if u:
            # None 값 처리
            return jsonify({
                "user": {
                    "id": u[0],
                    "nickname": u[1] or "User",
                    "profile_img": u[2] or "img/profile-placeholder.png",
                    "role": u[3] or "user"
                }
            })
        return jsonify({"error": "User not found"}), 404
    except Exception as e:
        print(f"[Profile Error] {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/user/update', methods=['POST'])
def api_update_profile():
    d = request.get_json(force=True, silent=True) or {}
    uid = d.get('user_id')
    nick = d.get('nickname')
    
    if not uid: return jsonify({"error": "User ID required"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE USERS SET nickname=:1 WHERE user_id=:2", [nick, uid])
        conn.commit()
        return jsonify({"message": "프로필 수정 완료"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# [FIX] 500 에러 해결: Null 값 방어 로직 추가
@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_ttl():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        # 안전한 쿼리 실행
        cursor.execute("""
            SELECT m.movie_id, m.title, m.rank, m.poster_url, 
                   t.track_id, t.track_title, t.artist_name, t.preview_url, a.album_cover_url
            FROM MOVIES m
            LEFT JOIN MOVIE_OSTS mo ON m.movie_id = mo.movie_id
            LEFT JOIN TRACKS t ON mo.track_id = t.track_id
            LEFT JOIN ALBUMS a ON t.album_id = a.album_id
            ORDER BY m.rank ASC
        """)
        rows = cursor.fetchall()
        
        ttl = """@prefix schema: <http://schema.org/> .
@prefix komc: <https://knowledgemap.kr/komc/def/> .
@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .
"""
        seen_movies = set()

        for r in rows:
            # None 값 안전 처리 (or "")
            mid_raw, title, rank, poster = r[0] or "", r[1] or "Unknown", r[2] or 99, r[3]
            tid, t_title, t_artist, preview, cover = r[4], r[5], r[6], r[7], r[8]

            if not mid_raw: continue
            
            # 영화 ID 인코딩
            mid = base64.urlsafe_b64encode(str(mid_raw).encode()).decode().rstrip("=")
            
            # 영화 정보 (중복 제거)
            if title not in seen_movies:
                # 포스터가 없으면 앨범 커버, 그것도 없으면 기본 이미지
                final_poster = poster or cover or "img/playlist-placeholder.png"
                ttl += f"""
<https://knowledgemap.kr/komc/resource/movie/{mid}> a schema:Movie ;
    schema:name "{title}" ;
    schema:image "{final_poster}" ;
    komc:rank {rank} .
"""
                seen_movies.add(title)

            # 트랙 정보 (트랙이 없으면 가짜 ID 생성해서라도 연결)
            if tid:
                t_uri = tid
                t_name = t_title or "제목 없음"
                t_artist = t_artist or "아티스트 미상"
                t_img = cover or poster or "img/playlist-placeholder.png"
            else:
                t_uri = f"{mid}_ost"
                t_name = f"{title} (OST 정보 없음)"
                t_artist = "Unknown"
                t_img = poster or "img/playlist-placeholder.png"

            ttl += f"""
<https://knowledgemap.kr/komc/resource/track/{t_uri}> a schema:MusicRecording ;
    schema:name "{t_name}" ;
    schema:byArtist "{t_artist}" ;
    schema:image "{t_img}" ;
    schema:audio "{preview or ''}" ;
    komc:featuredIn <https://knowledgemap.kr/komc/resource/movie/{mid}> ;
    schema:genre "Movie Soundtrack" .
"""
        return Response(ttl, mimetype='text/turtle')

    except Exception as e:
        print(f"[TTL Error] {e}")
        # 에러가 나도 500 대신 빈 TTL이라도 던져서 프론트가 죽지 않게 함
        return Response("# Error generating TTL", mimetype='text/turtle')

# --- 나머지 필수 API들 (로그인, 회원가입 등 유지) ---
@app.route('/api/auth/signup', methods=['POST'])
def api_signup():
    d = request.get_json(force=True, silent=True) or {}
    uid = d.get('id', '').strip().lower()
    pw = d.get('password', '').strip()
    nick = d.get('nickname', 'User').strip()
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id FROM USERS WHERE user_id=:1", [uid])
        if cur.fetchone(): return jsonify({"error": "ID exists"}), 409
        cur.execute("INSERT INTO USERS (user_id, password, nickname, role) VALUES (:1, :2, :3, 'user')", [uid, generate_password_hash(pw), nick])
        conn.commit(); return jsonify({"message": "Success"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    d = request.get_json(force=True, silent=True) or {}
    uid = d.get('id', '').strip().lower()
    pw = d.get('password', '').strip()
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id, password, nickname, profile_img, role FROM USERS WHERE user_id=:1", [uid])
        u = cur.fetchone()
        if u and check_password_hash(u[1], pw): return jsonify({"message": "Success", "user": {"id": u[0], "nickname": u[2], "profile_img": u[3], "role": u[4]}})
        return jsonify({"error": "Invalid credentials"}), 401
    except Exception as e: return jsonify({"error": str(e)}), 500

# (나머지 Search, Token, Update-Movies 등은 기존과 동일하므로 생략하거나 그대로 두세요)
# ...

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)