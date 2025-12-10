import os
import requests
import oracledb
import base64
import re
from flask import Flask, request, jsonify, g, send_from_directory, make_response
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, timedelta

# --- 설정 (Config) ---
UPLOAD_FOLDER = os.path.join(os.getcwd(), 'uploads')
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
KOBIS_API_KEY = os.getenv("KOBIS_API_KEY")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
DATA_GO_KR_API_KEY = os.getenv("DATA_GO_KR_API_KEY")

SPOTIFY_AUTH_URL = "https://accounts.spotify.com/api/token"
KOBIS_BOXOFFICE_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/boxoffice/searchDailyBoxOfficeList.json"
KOBIS_MOVIE_LIST_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/movie/searchMovieList.json"
WEATHER_API_URL = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst"
HOLIDAY_API_URL = "http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"

DB_USER = os.getenv("DB_USER", "admin")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
DB_DSN = os.getenv("DB_DSN", "ordb.mirinea.org:1521/XEPDB1")

PITCH_CLASS = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
CORS(app)

# --- DB 연결 (Pool) ---
db_pool = None

def init_db_pool():
    global db_pool
    try:
        db_pool = oracledb.create_pool(user=DB_USER, password=DB_PASSWORD, dsn=DB_DSN, min=1, max=5)
        print("✅ [DB] Oracle Pool 생성 완료.")
    except Exception as e:
        print(f"❌ [DB] Pool 생성 실패: {e}")

with app.app_context():
    init_db_pool()

def get_db_connection():
    if not db_pool: raise Exception("DB 연결 풀이 없습니다.")
    if 'db' not in g: g.db = db_pool.acquire()
    return g.db

@app.teardown_appcontext
def close_db(e):
    db = g.pop('db', None)
    if db: db.close()

# --- SKOS Manager (Optional) ---
try:
    from skos_manager import SkosManager
    skos_manager = SkosManager("skos-definition.ttl")
except:
    skos_manager = None

# --- 헬퍼 함수들 ---
def get_spotify_headers():
    if not SPOTIFY_CLIENT_ID: return {}
    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    try:
        res = requests.post(SPOTIFY_AUTH_URL, headers={'Authorization': f'Basic {b64_auth}', 'Content-Type': 'application/x-www-form-urlencoded'}, data={'grant_type': 'client_credentials'})
        return {'Authorization': f'Bearer {res.json().get("access_token")}'}
    except: return {}

def extract_spotify_id(url):
    if not url: return None
    match = re.search(r'track/([a-zA-Z0-9]{22})', url)
    return match.group(1) if match else None

def get_current_weather():
    # (공공데이터 날씨 로직 - 생략 시 Clear 반환)
    return "Clear"

def get_today_holiday():
    return None

def ms_to_iso_duration(ms):
    if not ms: return "PT0M0S"
    s = int((ms/1000)%60); m = int((ms/(1000*60))%60)
    return f"PT{m}M{s}S"

# --- 핵심 로직 (영화/트랙 저장) ---
def save_track_details(track_id, cursor, headers, genres=[]):
    if not track_id: return None
    try:
        t_res = requests.get(f"{SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers)
        if t_res.status_code != 200: return None
        t_data = t_res.json()
        a_res = requests.get(f"{SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers)
        a_data = a_res.json() if a_res.status_code == 200 else {}

        title = t_data.get('name', 'Unknown')
        artist = t_data['artists'][0]['name'] if t_data.get('artists') else 'Unknown'
        prev = t_data.get('preview_url', '')
        aid = t_data.get('album', {}).get('id')
        img = t_data.get('album', {}).get('images', [{}])[0].get('url', '')
        bpm = a_data.get('tempo', 0); k_int = a_data.get('key', -1)
        mkey = PITCH_CLASS[k_int] if 0 <= k_int < 12 else 'Unknown'
        dur = ms_to_iso_duration(t_data.get('duration_ms', 0))

        # 앨범 & 트랙
        if aid:
            cursor.execute("MERGE INTO ALBUMS USING dual ON (album_id=:1) WHEN NOT MATCHED THEN INSERT (album_id, album_cover_url) VALUES (:1, :2)", [aid, img])
        
        cursor.execute("""
            MERGE INTO TRACKS t USING dual ON (t.track_id=:tid)
            WHEN MATCHED THEN UPDATE SET t.image_url=:img, t.preview_url=:prev
            WHEN NOT MATCHED THEN INSERT (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, music_key, duration, views)
            VALUES (:tid, :title, :artist, :aid, :prev, :img, :bpm, :mkey, :dur, 0)
        """, {'tid':track_id, 'title':title, 'artist':artist, 'aid':aid, 'prev':prev, 'img':img, 'bpm':bpm, 'mkey':mkey, 'dur':dur})

        # 태그
        tags = set(["tag:Spotify"])
        g_map = {"액션":"tag:Action", "SF":"tag:SF", "코미디":"tag:Exciting", "드라마":"tag:Sentimental", "멜로":"tag:Romance", "로맨스":"tag:Romance", "공포":"tag:Tension", "호러":"tag:Tension", "스릴러":"tag:Tension", "범죄":"tag:Tension", "애니메이션":"tag:Animation", "가족":"tag:Rest", "뮤지컬":"tag:Pop"}
        for g in genres:
            for k,v in g_map.items(): 
                if k in g: tags.add(v)
        
        if a_data:
            e = a_data.get('energy', 0); v = a_data.get('valence', 0)
            if e>0.7: tags.add('tag:Exciting')
            if e<0.4: tags.add('tag:Rest')
            if v<0.3: tags.add('tag:Sentimental')
            if v>0.7: tags.add('tag:Pop')

        final_tags = set(tags)
        if skos_manager:
            for t in tags: final_tags.update(skos_manager.get_broader_tags(t))

        for t in final_tags:
            try: cursor.execute("MERGE INTO TRACK_TAGS t USING (SELECT :1 a, :2 b FROM dual) s ON (t.track_id=s.a AND t.tag_id=s.b) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (s.a, s.b)", [track_id, t])
            except: pass
        
        cursor.connection.commit()
        return t_data
    except Exception as e:
        print(f"[Save Error] {e}")
        return None

def update_box_office_data():
    print("[Batch] 박스오피스 업데이트...")
    conn = None
    try:
        conn = get_db_connection(); cursor = conn.cursor()
        target_dt = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        res = requests.get(KOBIS_BOXOFFICE_URL, params={"key": KOBIS_API_KEY, "targetDt": target_dt, "itemPerPage": "10"}).json()
        movie_list = res.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])
        
        for movie in movie_list:
            rank = int(movie['rank']); title = movie['movieNm']
            poster_url = None
            try:
                tmdb_res = requests.get("https://api.themoviedb.org/3/search/movie", params={"api_key": TMDB_API_KEY, "query": title, "language": "ko-KR"}).json()
                if tmdb_res.get('results'): poster_url = f"https://image.tmdb.org/t/p/w500{tmdb_res['results'][0]['poster_path']}"
            except: pass

            try:
                cursor.execute("""
                    MERGE INTO MOVIES m USING (SELECT :mid AS mid FROM dual) d 
                    ON (m.movie_id=d.mid) 
                    WHEN MATCHED THEN UPDATE SET rank=:rank, poster_url=:poster, title=:title 
                    WHEN NOT MATCHED THEN INSERT (movie_id, title, rank, poster_url) VALUES (:mid, :title, :rank, :poster)
                """, {'mid':movie['movieCd'], 'title':title, 'rank':rank, 'poster':poster_url})
                conn.commit()
            except: pass
        return "업데이트 완료"
    except Exception as e: return f"Error: {e}"
    finally:
        if conn: conn.close()

# --- API 라우트 ---

@app.route('/api/user/profile', methods=['POST'])
def api_get_profile():
    d = request.get_json(force=True, silent=True) or {}
    uid = d.get('user_id')
    if not uid: return jsonify({"error": "No User ID"}), 400
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id, nickname, profile_img, role FROM USERS WHERE user_id=:1", [uid])
        u = cur.fetchone()
        if u: return jsonify({"user": {"id":u[0], "nickname":u[1], "profile_img":u[2] or "img/profile-placeholder.png", "role":u[3]}})
        return jsonify({"error": "User not found"}), 404
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/user/update', methods=['POST'])
def api_update_profile():
    d = request.get_json(force=True, silent=True) or {}
    uid = d.get('user_id'); nick = d.get('nickname')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("UPDATE USERS SET nickname=:1 WHERE user_id=:2", [nick, uid])
        conn.commit()
        return jsonify({"message": "수정 완료"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_ttl():
    try:
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("""
            SELECT m.movie_id, m.title, m.rank, m.poster_url, 
                   t.track_id, t.track_title, t.artist_name, t.preview_url, a.album_cover_url
            FROM MOVIES m
            LEFT JOIN MOVIE_OSTS mo ON m.movie_id = mo.movie_id
            LEFT JOIN TRACKS t ON mo.track_id = t.track_id
            LEFT JOIN ALBUMS a ON t.album_id = a.album_id
            WHERE m.rank <= 10 ORDER BY m.rank ASC
        """)
        rows = cursor.fetchall()
        ttl = "@prefix schema: <http://schema.org/> .\n@prefix komc: <https://knowledgemap.kr/komc/def/> .\n@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .\n"
        seen = set()
        for r in rows:
            if not r[0] or r[1] in seen: continue
            seen.add(r[1])
            mid = base64.urlsafe_b64encode(str(r[0]).encode()).decode().rstrip("=")
            img = r[3] or r[8] or "img/playlist-placeholder.png"
            tid = r[4] or f"{mid}_ost"
            ttl += f"""
<https://knowledgemap.kr/komc/resource/movie/{mid}> a schema:Movie ; schema:name "{r[1]}" ; schema:image "{img}" ; komc:rank {r[2]} .
<https://knowledgemap.kr/komc/resource/track/{tid}> a schema:MusicRecording ; schema:name "{r[5] or 'OST 정보 없음'}" ; schema:byArtist "{r[6] or 'Unknown'}" ; schema:image "{img}" ; komc:featuredIn <https://knowledgemap.kr/komc/resource/movie/{mid}> .\n"""
        return Response(ttl, mimetype='text/turtle')
    except: return Response("# Error", mimetype='text/turtle')

@app.route('/api/movie/<mid>/update-ost', methods=['POST'])
def api_up_ost(mid):
    d = request.get_json(force=True, silent=True) or {}
    link = d.get('spotifyUrl'); uid = d.get('user_id')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        tid = extract_spotify_id(link)
        if not tid: return jsonify({"error": "링크 확인"}), 400
        headers = get_spotify_headers()
        res = save_track_details(tid, cur, headers, [])
        if not res: return jsonify({"error": "트랙 정보 없음"}), 404
        cur.execute("DELETE FROM MOVIE_OSTS WHERE movie_id=:1", [mid])
        cur.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:1, :2)", [mid, tid])
        cur.execute("INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, previous_value, new_value, user_id) VALUES ('MOVIE_OST', :1, 'UPDATE', 'NONE', :2, :3)", [mid, tid, uid])
        conn.commit()
        return jsonify({"message": "등록 완료!", "new_track": res['name']})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/track/<tid>/tags', methods=['POST'])
def api_add_tags(tid):
    d = request.get_json(force=True); tags = d.get('tags', [])
    try:
        conn = get_db_connection(); cur = conn.cursor()
        for tag in tags:
            tag = tag.strip()
            if not tag: continue
            if not tag.startswith('tag:'): tag = f"tag:{tag}"
            targets = {tag}
            if skos_manager: targets.update(skos_manager.get_broader_tags(tag))
            for t in targets:
                try: cursor.execute("MERGE INTO TRACK_TAGS t USING (SELECT :1 a, :2 b FROM dual) s ON (t.track_id=s.a AND t.tag_id=s.b) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (s.a, s.b)", [tid, t])
                except: pass
        conn.commit()
        return jsonify({"message": "태그 저장됨"})
    except: return jsonify({"error": "Error"}), 500

@app.route('/api/track/<tid>/tags', methods=['GET'])
def api_get_tags(tid):
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cursor.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id=:1", [tid])
        return jsonify([r[0].replace('tag:', '') for r in cursor.fetchall()])
    except: return jsonify([])

@app.route('/api/auth/signup', methods=['POST'])
def api_signup():
    d = request.get_json(force=True); uid = d.get('id'); pw = d.get('password'); nick = d.get('nickname')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("INSERT INTO USERS (user_id, password, nickname, role) VALUES (:1, :2, :3, 'user')", [uid, generate_password_hash(pw), nick])
        conn.commit(); return jsonify({"message": "Success"})
    except: return jsonify({"error": "Fail"}), 500

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    d = request.get_json(force=True); uid = d.get('id'); pw = d.get('password')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id, password, nickname, profile_img, role FROM USERS WHERE user_id=:1", [uid])
        u = cur.fetchone()
        if u and check_password_hash(u[1], pw): return jsonify({"user": {"id":u[0], "nickname":u[2], "profile_img":u[3], "role":u[4]}})
        return jsonify({"error": "Invalid"}), 401
    except: return jsonify({"error": "Error"}), 500

@app.route('/api/admin/update-movies', methods=['POST'])
def api_admin_update(): return jsonify({"message": update_box_office_data()})

@app.route('/api/spotify-token', methods=['GET'])
def api_token(): return jsonify({"access_token": get_spotify_headers().get('Authorization', '').split(' ')[1]})

@app.route('/api/search', methods=['GET'])
def api_search(): return jsonify(requests.get(f"{SPOTIFY_API_BASE}/search", headers=get_spotify_headers(), params={"q":request.args.get('q'),"type":"track","limit":20,"market":"KR"}).json())

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)