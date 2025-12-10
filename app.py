import os
import requests
import base64
import oracledb
import re
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, g, Response
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

# [선택] SKOS 매니저
try:
    from skos_manager import SkosManager
    skos_manager = SkosManager("skos-definition.ttl")
    print("✅ SKOS Manager 로드 성공")
except:
    print("⚠️ SKOS Manager 없음 (태그 확장 비활성화)")
    skos_manager = None

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
    print("🚨 필수 API 키 누락! docker-compose.yml 확인 필요")

DB_USER = os.getenv("DB_USER", "admin")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
DB_DSN = os.getenv("DB_DSN", "ordb.mirinea.org:1521/XEPDB1")

PITCH_CLASS = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

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
    if not SPOTIFY_CLIENT_ID: raise Exception("Spotify Key Missing")
    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    res = requests.post(SPOTIFY_auth_URL, headers={'Authorization': f'Basic {b64_auth}', 'Content-Type': 'application/x-www-form-urlencoded'}, data={'grant_type': 'client_credentials'})
    if res.status_code != 200: raise Exception(f"Auth Failed: {res.status_code}")
    return {'Authorization': f'Bearer {res.json().get("access_token")}'}

def ms_to_iso_duration(ms):
    if not ms: return "PT0M0S"
    s = int((ms/1000)%60); m = int((ms/(1000*60))%60)
    return f"PT{m}M{s}S"

def extract_spotify_id(url):
    if len(url) == 22 and re.match(r'^[a-zA-Z0-9]+$', url): return url
    match = re.search(r'track/([a-zA-Z0-9]{22})', url)
    return match.group(1) if match else None

# --- 3. 공공데이터 ---
def get_current_weather():
    if not DATA_GO_KR_API_KEY: return "Clear"
    now = datetime.now()
    base_date = now.strftime("%Y%m%d")
    if now.minute < 45: now -= timedelta(hours=1)
    params = {'serviceKey': DATA_GO_KR_API_KEY, 'pageNo': '1', 'numOfRows': '10', 'dataType': 'JSON', 'base_date': base_date, 'base_time': now.strftime("%H00"), 'nx': '60', 'ny': '127'}
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
        item_list = res.json().get('response', {}).get('body', {}).get('items', {}).get('item', [])
        if isinstance(item_list, dict): item_list = [item_list]
        today_str = now.strftime("%Y%m%d")
        for item in item_list:
            if str(item.get('locdate')) == today_str and item.get('isHoliday') == 'Y': return item.get('dateName')
        return None
    except: return None

# --- 4. 검색/저장 로직 ---
def get_kobis_metadata(movie_name):
    try:
        res = requests.get(KOBIS_MOVIE_LIST_URL, params={'key': KOBIS_API_KEY, 'movieNm': movie_name}).json()
        t = res.get('movieListResult', {}).get('movieList', [])[0]
        return (t.get('genreAlt', '').split(',') if t.get('genreAlt') else []), t.get('movieNmEn', ''), t.get('movieNmOg', '')
    except: return [], "", ""

def find_best_track(titles, headers):
    for title in set(titles):
        try:
            res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params={"q": f"{title} ost", "type": "track", "limit": 5, "market": "KR"}).json()
            for track in res.get('tracks', {}).get('items', []):
                if max(get_similarity(title, track['name']), get_similarity(title, track['album']['name'])) >= 0.5: return track
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
        
        final_tags = set(tags)
        if skos_manager:
            for t in tags: final_tags.update(skos_manager.get_broader_tags(t))

        for t in final_tags:
            try: cursor.execute("MERGE INTO TRACK_TAGS t USING (SELECT :1 AS tid, :2 AS tag FROM dual) s ON (t.track_id = s.tid AND t.tag_id = s.tag) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (s.tid, s.tag)", [track_id, t])
            except: pass
        
        cursor.connection.commit()
        return t_data
    except: return None

def update_box_office_data():
    print("[Batch] 업데이트 시작...")
    try:
        conn = get_db_connection(); cursor = conn.cursor(); headers = get_spotify_headers()
        target_dt = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        res = requests.get(KOBIS_BOXOFFICE_URL, params={"key": KOBIS_API_KEY, "targetDt": target_dt, "itemPerPage": "10"}).json()
        movie_list = res.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])
        
        for movie in movie_list:
            title = movie['movieNm']; rank = int(movie['rank'])
            genres, title_en, title_og = get_kobis_metadata(title)
            
            try:
                cursor.execute("MERGE INTO MOVIES m USING (SELECT :mid AS mid FROM dual) d ON (m.movie_id=d.mid) WHEN MATCHED THEN UPDATE SET rank=:rank WHEN NOT MATCHED THEN INSERT (movie_id, title, rank) VALUES (:mid, :title, :rank)", {'mid':title, 'title':title, 'rank':rank})
                conn.commit()
            except: pass

            matched = find_best_track([title_og, title_en, title], headers)
            if matched:
                tid = matched['id']
                save_track_details(tid, cursor, headers, genres)
                try:
                    cursor.execute("DELETE FROM MOVIE_OSTS WHERE movie_id=:mid", {'mid':title})
                    cursor.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:mid, :tid)", {'mid':title, 'tid':tid})
                    conn.commit()
                except: pass
        return "업데이트 완료"
    except Exception as e: return f"Error: {e}"

# --- API 라우트 ---

# [NEW] 태그 저장 API (권한 체크 포함)
@app.route('/api/track/<tid>/tags', methods=['POST'])
def api_add_tags(tid):
    data = request.json
    new_tags = data.get('tags', [])
    user_id = data.get('user_id')
    
    if not new_tags: return jsonify({"error": "태그가 없습니다."}), 400
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 권한 확인 (관리자만 가능하게 하려면 아래 주석 해제)
        cursor.execute("SELECT role FROM USERS WHERE user_id=:1", [user_id])
        user = cursor.fetchone()
        if not user or user[0] != 'admin':
             return jsonify({"error": "관리자만 태그를 추가할 수 있습니다."}), 403
            
        added_count = 0
        for tag in new_tags:
            tag = tag.strip()
            if not tag: continue
            # 'tag:' 접두사 자동 추가
            if not tag.startswith('tag:'): tag = f"tag:{tag}"
            
            # SKOS 확장
            tags_to_add = {tag}
            if skos_manager:
                tags_to_add.update(skos_manager.get_broader_tags(tag))
            
            for t in tags_to_add:
                try:
                    cursor.execute("MERGE INTO TRACK_TAGS t USING (SELECT :1 AS tid, :2 AS tag FROM dual) s ON (t.track_id = s.tid AND t.tag_id = s.tag) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (s.tid, s.tag)", [tid, t])
                    added_count += 1
                except: pass
        
        conn.commit()
        return jsonify({"message": f"{added_count}개 태그 저장 완료"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# [NEW] 태그 조회 API (팝업 표시용)
@app.route('/api/track/<tid>/tags', methods=['GET'])
def api_get_tags(tid):
    try:
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id=:1", [tid])
        tags = [r[0].replace('tag:', '') for r in cursor.fetchall()]
        return jsonify(tags)
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/spotify-token', methods=['GET'])
def api_get_token():
    try: return jsonify({"access_token": get_spotify_headers()['Authorization'].split(' ')[1]})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/search', methods=['GET'])
def api_search():
    query = request.args.get('q', '')
    if not query: return jsonify({"error": "Query required"}), 400
    try:
        res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=get_spotify_headers(), params={"q": query, "type": "track", "limit": 20, "market": "KR"})
        return jsonify(res.json())
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/track/<tid>', methods=['GET'])
def api_track(tid):
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT track_title, artist_name, image_url, bpm, music_key, duration FROM TRACKS WHERE track_id=:1", [tid])
        r = cur.fetchone()
        if r and r[3]: return jsonify({"id":tid, "title":r[0], "artist":r[1], "image":r[2], "bpm":r[3], "key":r[4], "duration":r[5], "source":"DB"})
        save_track_details(tid, cur, get_spotify_headers(), [])
        return jsonify({"message": "Fetched from Spotify"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/recommend/context', methods=['GET'])
def api_recommend_context():
    try:
        weather = get_current_weather() or "Clear"
        holiday = get_today_holiday()
        target_tags = ['tag:Exciting', 'tag:Pop'] if holiday else (['tag:Sentimental'] if weather=="Rain" else ['tag:Exciting'])
        conn = get_db_connection(); cursor = conn.cursor()
        bind_vars = {f't{i}': t for i, t in enumerate(target_tags)}
        placeholders = ', '.join([f':t{i}' for i in range(len(target_tags))])
        cursor.execute(f"SELECT t.track_title, t.artist_name, t.image_url, t.preview_url FROM TRACKS t JOIN TRACK_TAGS tt ON t.track_id = tt.track_id WHERE tt.tag_id IN ({placeholders}) ORDER BY DBMS_RANDOM.VALUE FETCH FIRST 6 ROWS ONLY", bind_vars)
        tracks = [{"title": r[0], "artist": r[1], "cover": r[2], "preview": r[3]} for r in cursor.fetchall()]
        return jsonify({"message": f"오늘 날씨: {weather}", "tracks": tracks, "tags": [t.replace('tag:', '') for t in target_tags]})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/auth/signup', methods=['POST'])
def api_signup():
    d = request.json; uid = d.get('id', '').strip().lower(); pw = d.get('password', '').strip(); nick = d.get('nickname', 'User').strip()
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id FROM USERS WHERE user_id=:1", [uid])
        if cur.fetchone(): return jsonify({"error": "ID exists"}), 409
        cur.execute("INSERT INTO USERS (user_id, password, nickname, role) VALUES (:1, :2, :3, 'user')", [uid, generate_password_hash(pw), nick])
        conn.commit(); return jsonify({"message": "Success"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/auth/login', methods=['POST'])
def api_login():
    d = request.json; uid = d.get('id', '').strip().lower(); pw = d.get('password', '').strip()
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT user_id, password, nickname, profile_img, role FROM USERS WHERE user_id=:1", [uid])
        u = cur.fetchone()
        if u and check_password_hash(u[1], pw): return jsonify({"message": "Success", "user": {"id": u[0], "nickname": u[2], "profile_img": u[3], "role": u[4]}})
        return jsonify({"error": "Invalid credentials"}), 401
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/admin/logs', methods=['POST'])
def api_logs():
    d = request.json; uid = d.get('user_id')
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT role FROM USERS WHERE user_id=:1", [uid])
        r = cur.fetchone()
        if not r or r[0] != 'admin': return jsonify({"error": "No permission"}), 403
        cur.execute("SELECT target_id, previous_value, new_value, user_id, created_at, user_ip FROM MODIFICATION_LOGS ORDER BY created_at DESC FETCH FIRST 50 ROWS ONLY")
        logs = [{"movie":r[0], "old":r[1], "new":r[2], "user":r[3], "date":r[4].strftime("%Y-%m-%d %H:%M"), "ip":r[5]} for r in cur.fetchall()]
        return jsonify(logs)
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/movie/<mid>/update-ost', methods=['POST'])
def api_up_ost(mid):
    d = request.json; link = d.get('spotifyUrl'); uid = d.get('user_id', 'Guest'); ip = request.remote_addr
    try:
        conn = get_db_connection(); cur = conn.cursor()
        real_mid = mid # (디코딩 생략)
        tid = extract_spotify_id(link)
        if not tid: return jsonify({"error": "Invalid Link"}), 400
        save_track_details(tid, cur, get_spotify_headers(), [])
        cur.execute("DELETE FROM MOVIE_OSTS WHERE movie_id=:mid", {'mid':real_mid})
        cur.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:mid, :tid)", {'mid':real_mid, 'tid':tid})
        cur.execute("INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, previous_value, new_value, user_ip, user_id) VALUES ('MOVIE_OST', :1, 'UPDATE', 'NONE', :2, :3, :4)", [real_mid, tid, ip, uid])
        conn.commit()
        return jsonify({"message": "Updated"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/admin/update-movies', methods=['POST'])
def api_adm_update(): return jsonify({"message": update_box_office_data()})

@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_ttl():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # [핵심] 영화와 OST 정보를 조인해서 가져옴 (랭킹 순)
        # 영화 정보만 있고 OST가 없어도 영화는 나오도록 LEFT JOIN 사용
        query = """
            SELECT m.movie_id, m.title, m.rank, m.poster_url, 
                   t.track_id, t.track_title, t.artist_name, t.preview_url, a.album_cover_url
            FROM MOVIES m
            LEFT JOIN MOVIE_OSTS mo ON m.movie_id = mo.movie_id
            LEFT JOIN TRACKS t ON mo.track_id = t.track_id
            LEFT JOIN ALBUMS a ON t.album_id = a.album_id
            WHERE m.rank IS NOT NULL  -- 랭킹이 있는 영화만 조회
            ORDER BY m.rank ASC
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        
        ttl = """@prefix schema: <http://schema.org/> .
@prefix komc: <https://knowledgemap.kr/komc/def/> .
@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .
"""
        tag_cursor = conn.cursor()

        # 중복 방지를 위한 세트
        processed_movies = set()

        for row in rows:
            mid, mtitle, rank, mposter, tid, ttitle, artist, preview, cover = row
            
            # 영화 ID 인코딩 (URL 안전하게)
            m_uri = base64.urlsafe_b64encode(mid.encode()).decode().rstrip("=")
            
            # [중요] 영화 정보는 한 번만 정의
            if mid not in processed_movies:
                mposter = mposter or "img/playlist-placeholder.png"
                ttl += f"""
<https://knowledgemap.kr/komc/resource/movie/{m_uri}> a schema:Movie ;
    schema:name "{mtitle}" ;
    schema:image "{mposter}" ;
    komc:rank {rank} .
"""
                processed_movies.add(mid)

            # [중요] 트랙 정보 정의 (영화와 연결)
            # 트랙이 없으면(tid is None) 가상의 OST 정보를 만들어서라도 연결해줌
            t_uri_suffix = tid if tid else f"{m_uri}_ost"
            ttitle = ttitle or f"{mtitle} (OST 정보 없음)"
            artist = artist or "Unknown Artist"
            cover = cover or mposter # 앨범 커버 없으면 영화 포스터 사용
            preview = preview or ""

            # 태그 조회
            tags_str = ""
            if tid:
                try:
                    tag_cursor.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id = :1", [tid])
                    tags = [t[0].replace('tag:', '') for t in tag_cursor.fetchall()]
                    if tags:
                        tags_str = f"    komc:relatedTag tag:{', tag:'.join(tags)} ;"
                except: pass

            ttl += f"""
<https://knowledgemap.kr/komc/resource/track/{t_uri_suffix}> a schema:MusicRecording ;
    schema:name "{ttitle}" ;
    schema:byArtist "{artist}" ;
    schema:image "{cover}" ;
    schema:audio "{preview}" ;
    komc:featuredIn <https://knowledgemap.kr/komc/resource/movie/{m_uri}> ;
{tags_str}
    schema:genre "Movie Soundtrack" .
"""
        return Response(ttl, mimetype='text/turtle')
        
    except Exception as e:
        print(f"TTL Error: {e}")
        return f"# Error generating TTL: {e}", 500
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT m.movie_id, m.title, m.rank, m.poster_url, t.track_id, t.track_title, t.artist_name, t.preview_url, a.album_cover_url FROM MOVIES m LEFT JOIN MOVIE_OSTS mo ON m.movie_id=mo.movie_id LEFT JOIN TRACKS t ON mo.track_id=t.track_id LEFT JOIN ALBUMS a ON t.album_id=a.album_id WHERE m.rank<=10 ORDER BY m.rank ASC")
        rows = cur.fetchall()
        ttl = "@prefix schema: <http://schema.org/> .\n@prefix komc: <https://knowledgemap.kr/komc/def/> .\n@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .\n"
        for r in rows:
            mid = base64.urlsafe_b64encode(r[0].encode()).decode().rstrip("="); tid = r[4] or f"{mid}_ost"
            ttl += f"<https://knowledgemap.kr/komc/resource/movie/{mid}> a schema:Movie ; schema:name \"{r[1]}\" .\n<https://knowledgemap.kr/komc/resource/track/{tid}> a schema:MusicRecording ; komc:featuredIn <https://knowledgemap.kr/komc/resource/movie/{mid}> .\n"
        return Response(ttl, mimetype='text/turtle')
    except Exception as e: return f"# Error: {e}", 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)