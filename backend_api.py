import os
import requests
import base64
import oracledb
import re
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, g, Response
from flask_cors import CORS

# --- 1. 설정 ---
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
KOBIS_API_KEY = os.getenv("KOBIS_API_KEY")
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
DATA_GO_KR_API_KEY = os.getenv("DATA_GO_KR_API_KEY")

SPOTIFY_auth_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

KOBIS_BOXOFFICE_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/boxoffice/searchDailyBoxOfficeList.json"
KOBIS_MOVIE_INFO_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/movie/searchMovieInfo.json"
KOBIS_MOVIE_LIST_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/movie/searchMovieList.json"
WEATHER_API_URL = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getUltraSrtNcst"
HOLIDAY_API_URL = "http://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"

if not all([SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, KOBIS_API_KEY, TMDB_API_KEY]):
    print("🚨 [CRITICAL] API 키 설정 누락! docker-compose.yml을 확인하세요.")

DB_USER = os.getenv("DB_USER", "admin")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
DB_DSN = os.getenv("DB_DSN", "ordb.mirinea.org:1521/XEPDB1")

PITCH_CLASS = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

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

# --- 2. 헬퍼 함수 ---
def clean_text(text):
    if not text: return ""
    text = text.lower()
    patterns = [r'\(.*?ost.*?\)', r'original motion picture soundtrack', r'soundtrack', r'ost']
    for pat in patterns: text = re.sub(pat, '', text)
    text = re.sub(r'[^a-z0-9가-힣\s]', ' ', text)
    return ' '.join(text.split())

def get_similarity(a, b):
    return SequenceMatcher(None, clean_text(a), clean_text(b)).ratio()

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
    if res.status_code != 200: raise Exception(f"Spotify Auth Failed: {res.status_code}")
    return {'Authorization': f'Bearer {res.json().get("access_token")}'}

def ms_to_iso_duration(ms):
    if not ms: return "PT0M0S"
    seconds = int((ms / 1000) % 60)
    minutes = int((ms / (1000 * 60)) % 60)
    return f"PT{minutes}M{seconds}S"

def extract_spotify_id(url):
    if len(url) == 22 and re.match(r'^[a-zA-Z0-9]+$', url): return url
    match = re.search(r'track/([a-zA-Z0-9]{22})', url)
    return match.group(1) if match else None

# --- 3. 공공데이터 조회 ---
def get_current_weather():
    if not DATA_GO_KR_API_KEY: return None
    now = datetime.now()
    base_date = now.strftime("%Y%m%d")
    if now.minute < 45: now -= timedelta(hours=1)
    base_time = now.strftime("%H00")
    params = {'serviceKey': DATA_GO_KR_API_KEY, 'pageNo': '1', 'numOfRows': '10', 'dataType': 'JSON', 'base_date': base_date, 'base_time': base_time, 'nx': '60', 'ny': '127'}
    try:
        res = requests.get(WEATHER_API_URL, params=params, timeout=5)
        items = res.json().get('response', {}).get('body', {}).get('items', {}).get('item', [])
        pty = next((item['obsrValue'] for item in items if item['category'] == 'PTY'), "0")
        if pty in ["1", "5", "2", "6"]: return "Rain"
        if pty in ["3", "7"]: return "Snow"
        return "Clear"
    except: return "Clear"

def get_today_holiday():
    if not DATA_GO_KR_API_KEY: return None
    now = datetime.now()
    params = {'serviceKey': DATA_GO_KR_API_KEY, 'solYear': now.year, 'solMonth': f"{now.month:02d}", '_type': 'json'}
    try:
        res = requests.get(HOLIDAY_API_URL, params=params, timeout=5)
        items = res.json().get('response', {}).get('body', {}).get('items', {})
        item_list = items.get('item', [])
        if isinstance(item_list, dict): item_list = [item_list]
        today_str = now.strftime("%Y%m%d")
        for item in item_list:
            if str(item.get('locdate')) == today_str and item.get('isHoliday') == 'Y':
                return item.get('dateName')
        return None
    except: return None

# --- 4. KOBIS & Spotify ---
def get_kobis_metadata(movie_name):
    params = {'key': KOBIS_API_KEY, 'movieNm': movie_name}
    try:
        res = requests.get(KOBIS_MOVIE_LIST_URL, params=params).json()
        mlist = res.get('movieListResult', {}).get('movieList', [])
        if mlist:
            t = mlist[0]
            return (t.get('genreAlt', '').split(',') if t.get('genreAlt') else []), t.get('movieNmEn', ''), t.get('movieNmOg', '')
        return [], "", ""
    except: return [], "", ""

def find_best_track(titles, headers):
    candidates = []
    seen = set()
    for t in titles:
        if t and t not in seen: candidates.append(t); seen.add(t)
    for title in candidates:
        try:
            res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params={"q": f"{title} ost", "type": "track", "limit": 5, "market": "KR"}).json()
            for track in res.get('tracks', {}).get('items', []):
                sim = max(get_similarity(title, track['name']), get_similarity(title, track['album']['name']))
                if sim >= 0.5: return track
        except: pass
    return None

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
        bpm = a_data.get('tempo', 0); k_int = a_data.get('key', -1); dur = ms_to_iso_duration(t_data.get('duration_ms', 0))
        mkey = PITCH_CLASS[k_int] if 0 <= k_int < 12 else 'Unknown'

        if aid:
            cursor.execute("MERGE INTO ALBUMS USING dual ON (album_id=:1) WHEN NOT MATCHED THEN INSERT (album_id, album_cover_url) VALUES (:1, :2)", [aid, img])
        
        cursor.execute("""
            MERGE INTO TRACKS t USING dual ON (t.track_id=:tid)
            WHEN MATCHED THEN UPDATE SET t.bpm=:bpm, t.music_key=:mkey, t.duration=:dur, t.image_url=:img
            WHEN NOT MATCHED THEN INSERT (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, music_key, duration)
            VALUES (:tid, :title, :artist, :aid, :prev, :img, :bpm, :mkey, :dur)
        """, {'tid':track_id, 'title':title, 'artist':artist, 'aid':aid, 'prev':prev, 'img':img, 'bpm':bpm, 'mkey':mkey, 'dur':dur})

        tags = set(["tag:Spotify"])
        if genres: tags.add("tag:MovieOST")
        e = a_data.get('energy', 0); v = a_data.get('valence', 0)
        if e>0.7: tags.add('tag:Exciting')
        if e<0.4: tags.add('tag:Rest')
        if v<0.3: tags.add('tag:Sentimental')
        if v>0.7: tags.add('tag:Pop')
        
        g_map = {"액션":"tag:Action", "SF":"tag:SF", "코미디":"tag:Exciting", "드라마":"tag:Sentimental", "멜로":"tag:Romance", "로맨스":"tag:Romance", "공포":"tag:Tension", "호러":"tag:Tension", "스릴러":"tag:Tension", "범죄":"tag:Tension", "애니메이션":"tag:Animation", "가족":"tag:Rest", "뮤지컬":"tag:Pop"}
        for g in genres:
            for k,val in g_map.items(): 
                if k in g: tags.add(val)
        
        for t in tags:
            try: cursor.execute("MERGE INTO TRACK_TAGS USING dual ON (track_id=:1 AND tag_id=:2) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (:1, :2)", [track_id, t])
            except: pass
        
        cursor.connection.commit()
        return t_data
    except Exception as e: return None

def update_box_office_data():
    # ... (기존과 동일)
    print("[Batch] 박스오피스 업데이트 시작...")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        headers = get_spotify_headers()
        target_dt = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        res = requests.get(KOBIS_BOXOFFICE_URL, params={"key": KOBIS_API_KEY, "targetDt": target_dt, "itemPerPage": "10"}).json()
        movie_list = res.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])
        if not movie_list: return "데이터 없음"
        for movie in movie_list:
            rank = int(movie['rank']); title = movie['movieNm']
            print(f"Processing: {title}")
            genres, title_en, title_og = get_kobis_metadata(title)
            poster_url = None
            try:
                tmdb_res = requests.get("https://api.themoviedb.org/3/search/movie", params={"api_key": TMDB_API_KEY, "query": title, "language": "ko-KR"}).json()
                if tmdb_res.get('results'): poster_url = f"https://image.tmdb.org/t/p/w500{tmdb_res['results'][0]['poster_path']}"
            except: pass
            try:
                cursor.execute("MERGE INTO MOVIES m USING (SELECT :mid AS mid FROM dual) d ON (m.movie_id=d.mid) WHEN MATCHED THEN UPDATE SET rank=:rank, poster_url=:poster WHEN NOT MATCHED THEN INSERT (movie_id, title, rank, poster_url) VALUES (:mid, :title, :rank, :poster)", {'mid':title, 'title':title, 'rank':rank, 'poster':poster_url})
                conn.commit()
            except: pass
            matched_track = find_best_track([title_og, title_en, title], headers)
            if matched_track:
                tid = matched_track['id']
                save_track_details(tid, cursor, headers, genres)
                try:
                    cursor.execute("DELETE FROM MOVIE_OSTS WHERE movie_id=:mid", {'mid':title})
                    cursor.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:mid, :tid)", {'mid':title, 'tid':tid})
                    conn.commit()
                except: pass
        return "업데이트 완료"
    except Exception as e: return f"Error: {e}"

# --- API ---
# [수정됨] 상황 기반 추천 (태그 목록도 함께 반환)
@app.route('/api/recommend/context', methods=['GET'])
def api_recommend_context():
    try:
        weather = get_current_weather() or "Clear"
        holiday = get_today_holiday()
        
        target_tags = []
        context_msg = ""

        if holiday:
            context_msg = f"🎉 오늘은 {holiday}입니다! 신나는 음악 어때요?"
            target_tags = ['tag:Exciting', 'tag:Pop']
        elif weather == "Rain":
            context_msg = "☔ 비가 오네요. 감성적인 음악을 준비했어요."
            target_tags = ['tag:Sentimental', 'tag:Rest']
        elif weather == "Snow":
            context_msg = "❄️ 눈이 내립니다. 로맨틱한 음악을 들어보세요."
            target_tags = ['tag:Romance', 'tag:Sentimental']
        else:
            context_msg = "☀️ 맑은 날씨엔 드라이브 음악이죠!"
            target_tags = ['tag:Exciting', 'tag:Pop']

        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 태그에 맞는 곡 조회 (랜덤)
        bind_vars = {f't{i}': t for i, t in enumerate(target_tags)}
        placeholders = ', '.join([f':t{i}' for i in range(len(target_tags))])
        
        query = f"""
            SELECT t.track_title, t.artist_name, t.image_url, t.preview_url
            FROM TRACKS t
            JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
            WHERE tt.tag_id IN ({placeholders})
            ORDER BY DBMS_RANDOM.VALUE
            FETCH FIRST 6 ROWS ONLY
        """
        cursor.execute(query, bind_vars)
        
        tracks = []
        for row in cursor.fetchall():
            tracks.append({"title": row[0], "artist": row[1], "cover": row[2], "preview": row[3]})
            
        return jsonify({
            "message": context_msg,
            "weather": weather,
            "holiday": holiday,
            "tracks": tracks,
            # [NEW] 태그 목록도 함께 전송 (tag: 접두사 제거)
            "tags": [t.replace('tag:', '') for t in target_tags]
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# (나머지 API들 그대로 유지)
@app.route('/api/auth/signup', methods=['POST'])
def api_signup():
    d = request.json
    # 1. 입력값 정제 (양쪽 공백 제거)
    uid = d.get('id', '').strip().lower()  # 아이디는 무조건 소문자로 통일
    pw = d.get('password', '').strip()     # 비밀번호 공백 제거
    nick = d.get('nickname', 'User').strip()

    if not uid or not pw:
        return jsonify({"error": "아이디와 비밀번호를 입력해주세요."}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 2. 중복 확인 (소문자로 변환된 ID로 확인하므로 중복 방지됨)
        cursor.execute("SELECT user_id FROM USERS WHERE user_id=:1", [uid])
        if cursor.fetchone():
            return jsonify({"error": "이미 존재하는 ID입니다."}), 409

        # 3. 저장
        cursor.execute("""
            INSERT INTO USERS (user_id, password, nickname, role) 
            VALUES (:1, :2, :3, 'user')
        """, [uid, generate_password_hash(pw), nick])
        
        conn.commit()
        return jsonify({"message": "회원가입 성공!"})

    except Exception as e:
        print(f"[회원가입 오류] {e}")
        return jsonify({"error": "서버 오류가 발생했습니다."}), 500


# ---------------------------------------------------------
# [수정] 로그인 API (공백 제거 + 소문자 변환)
# ---------------------------------------------------------
@app.route('/api/auth/login', methods=['POST'])
def api_login():
    d = request.json
    # 로그인할 때도 똑같이 정제해야 DB에 있는 값과 매칭됨
    uid = d.get('id', '').strip().lower()
    pw = d.get('password', '').strip()

    if not uid or not pw:
         return jsonify({"error": "아이디와 비밀번호를 입력해주세요."}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 사용자 조회
        cursor.execute("SELECT user_id, password, nickname, profile_img, role FROM USERS WHERE user_id=:1", [uid])
        user = cursor.fetchone() # (id, pw_hash, nickname, img, role)

        # 비밀번호 검증
        if user and check_password_hash(user[1], pw):
            return jsonify({
                "message": "로그인 성공",
                "user": {
                    "id": user[0],
                    "nickname": user[2],
                    "profile_img": user[3],
                    "role": user[4]
                }
            })
        else:
            # 아이디가 없거나 비번이 틀린 경우
            return jsonify({"error": "아이디 또는 비밀번호가 잘못되었습니다."}), 401

    except Exception as e:
        print(f"[로그인 오류] {e}")
        return jsonify({"error": "서버 오류가 발생했습니다."}), 500
@app.route('/api/admin/logs', methods=['POST'])
def api_logs():
    d = request.json; uid = d.get('user_id')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT role FROM USERS WHERE user_id=:1", [uid])
        if cur.fetchone()[0] != 'admin': return jsonify({"error": "No permission"}), 403
        cur.execute("SELECT target_id, previous_value, new_value, user_id, created_at, user_ip FROM MODIFICATION_LOGS ORDER BY created_at DESC FETCH FIRST 50 ROWS ONLY")
        logs = [{"movie":r[0], "old":r[1], "new":r[2], "user":r[3], "date":r[4].strftime("%Y-%m-%d %H:%M"), "ip":r[5]} for r in cur.fetchall()]
        return jsonify(logs)
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/movie/<mid>/update-ost', methods=['POST'])
def api_up_ost(mid):
    d = request.json; link = d.get('spotifyUrl'); uid = d.get('user_id', 'Guest'); ip = request.remote_addr
    if not link: return jsonify({"error": "Link required"}), 400
    try:
        conn = get_db_connection(); cur = conn.cursor(); headers = get_spotify_headers()
        real_mid = mid
        try:
            if mid.endswith('_ost'): mid = mid[:-4]
            pad = len(mid)%4; 
            if pad: mid += '='*(4-pad)
            dec = base64.urlsafe_b64decode(mid).decode('utf-8')
            cur.execute("SELECT count(*) FROM MOVIES WHERE movie_id=:1", [dec])
            if cur.fetchone()[0]>0: real_mid = dec
        except: pass
        tid = extract_spotify_id(link)
        if not tid: return jsonify({"error": "Invalid Link"}), 400
        res = save_track_details(tid, cur, headers, [])
        if not res: return jsonify({"error": "Track not found"}), 404
        cur.execute("SELECT track_id FROM MOVIE_OSTS WHERE movie_id=:1", [real_mid])
        prev = cur.fetchone(); prev_id = prev[0] if prev else "NONE"
        cur.execute("DELETE FROM MOVIE_OSTS WHERE movie_id=:1", [real_mid])
        cur.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:1, :2)", [real_mid, tid])
        cur.execute("INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, previous_value, new_value, user_ip, user_id) VALUES ('MOVIE_OST', :1, 'UPDATE', :2, :3, :4, :5)", [real_mid, prev_id, tid, ip, uid])
        conn.commit()
        return jsonify({"message": "Updated", "new_track": res['name']})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/admin/update-movies', methods=['POST'])
def api_adm_update(): return jsonify({"message": update_box_office_data()})

@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_ttl():
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("""SELECT m.movie_id, m.title, m.rank, m.poster_url, t.track_id, t.track_title, t.artist_name, t.preview_url, a.album_cover_url FROM MOVIES m LEFT JOIN MOVIE_OSTS mo ON m.movie_id=mo.movie_id LEFT JOIN TRACKS t ON mo.track_id=t.track_id LEFT JOIN ALBUMS a ON t.album_id=a.album_id WHERE m.rank<=10 ORDER BY m.rank ASC""")
        rows = cur.fetchall()
        ttl = "@prefix schema: <http://schema.org/> .\n@prefix komc: <https://knowledgemap.kr/komc/def/> .\n@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .\n"
        tcur = conn.cursor()
        for r in rows:
            mid, mt, rk, mp, tid, tt, ar, pr, cov = r
            m_uri = base64.urlsafe_b64encode(mid.encode()).decode().rstrip("=")
            mp = mp or "img/playlist-placeholder.png"; cov = cov or "img/playlist-placeholder.png"; tt = tt or "OST 정보 없음"; ar = ar or "-"
            tags = ""
            if tid:
                tcur.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id=:1", [tid])
                tl = [x[0].replace('tag:', '') for x in tcur.fetchall()]
                if tl: tags = f"    komc:relatedTag tag:{', tag:'.join(tl)} ;"
            t_uri = tid if tid else f"{m_uri}_ost"
            ttl += f"""<https://knowledgemap.kr/komc/resource/movie/{m_uri}> a schema:Movie ; schema:name "{mt}" ; schema:image "{mp}" ; komc:rank {rk} .\n<https://knowledgemap.kr/komc/resource/track/{t_uri}> a schema:MusicRecording ; schema:name "{tt}" ; schema:byArtist "{ar}" ; schema:image "{cov}" ; schema:audio "{pr or ''}" ; komc:featuredIn <https://knowledgemap.kr/komc/resource/movie/{m_uri}> ;\n{tags}\n    schema:genre "Movie Soundtrack" .\n"""
        return Response(ttl, mimetype='text/turtle')
    except Exception as e: return f"# Error: {e}", 500

@app.route('/api/spotify-token', methods=['GET'])
def api_tk(): return api_get_token()
@app.route('/api/search', methods=['GET'])
def api_src(): return api_search()
@app.route('/api/track/<tid>', methods=['GET'])
def api_tr(tid): return api_get_track_detail(tid)

from werkzeug.security import generate_password_hash, check_password_hash

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)