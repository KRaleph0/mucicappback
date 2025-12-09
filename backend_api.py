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

# Spotify 공식 API
SPOTIFY_auth_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

# KOBIS API
KOBIS_BOXOFFICE_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/boxoffice/searchDailyBoxOfficeList.json"
KOBIS_MOVIE_INFO_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/movie/searchMovieInfo.json"
KOBIS_MOVIE_LIST_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/movie/searchMovieList.json"

if not all([SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, KOBIS_API_KEY, TMDB_API_KEY]):
    print("🚨 [CRITICAL] API 키 설정 누락! docker-compose.yml을 확인하세요.")

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
    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    res = requests.post(SPOTIFY_auth_URL, 
                        headers={'Authorization': f'Basic {b64_auth}', 'Content-Type': 'application/x-www-form-urlencoded'}, 
                        data={'grant_type': 'client_credentials'})
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

# --- 3. KOBIS 상세 조회 (원제 포함) ---
def get_kobis_metadata(movie_name):
    params = {'key': KOBIS_API_KEY, 'movieNm': movie_name}
    try:
        response = requests.get(KOBIS_MOVIE_LIST_URL, params=params)
        data = response.json()
        movie_list = data.get('movieListResult', {}).get('movieList', [])
        
        if movie_list:
            target = movie_list[0]
            title_en = target.get('movieNmEn', '')
            title_og = target.get('movieNmOg', '') # 원제 추가
            genre_str = target.get('genreAlt', '')
            genres = genre_str.split(',') if genre_str else []
            
            print(f"    🔍 KOBIS 정보: {movie_name} (En: {title_en}, Og: {title_og}) / 장르: {genres}")
            return genres, title_en, title_og
        return [], "", ""
    except Exception as e:
        print(f"    ⚠️ KOBIS 오류: {e}")
        return [], "", ""

# --- 4. Spotify 검색 (3단계 + 유사도) ---
def find_best_track(titles, headers):
    search_candidates = []
    seen = set()
    for t in titles:
        if t and t not in seen:
            search_candidates.append(t)
            seen.add(t)

    for title in search_candidates:
        query = f"{title} ost"
        print(f"    🎵 검색 시도: '{query}'")
        try:
            res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params={"q": query, "type": "track", "limit": 5, "market": "KR"}).json()
            tracks = res.get('tracks', {}).get('items', [])
            
            for track in tracks:
                sim = max(get_similarity(title, track['name']), get_similarity(title, track['album']['name']))
                if sim >= 0.5:
                    print(f"      ✅ 매칭 성공! (유사도: {sim:.2f}) - {track['name']}")
                    return track
        except: pass
    return None

# --- 5. 트랙 저장 ---
def save_track_details(track_id, cursor, headers, genres=[]):
    if not track_id: return None
    try:
        track_res = requests.get(f"{SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers)
        if track_res.status_code != 200: return None
        t_data = track_res.json()
        
        audio_res = requests.get(f"{SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers)
        a_data = audio_res.json() if audio_res.status_code == 200 else {}

        title = t_data.get('name', 'Unknown')
        artist = t_data['artists'][0]['name'] if t_data.get('artists') else 'Unknown'
        preview = t_data.get('preview_url', '')
        album_id = t_data.get('album', {}).get('id')
        image_url = t_data.get('album', {}).get('images', [{}])[0].get('url', '')
        
        bpm = a_data.get('tempo', 0)
        key_int = a_data.get('key', -1)
        music_key = PITCH_CLASS[key_int] if 0 <= key_int < len(PITCH_CLASS) else 'Unknown'
        duration_iso = ms_to_iso_duration(t_data.get('duration_ms', 0))

        if album_id:
            cursor.execute("MERGE INTO ALBUMS USING dual ON (album_id = :aid) WHEN NOT MATCHED THEN INSERT (album_id, album_cover_url) VALUES (:aid, :cover)", {'aid': album_id, 'cover': image_url})

        cursor.execute("""
            MERGE INTO TRACKS t USING dual ON (t.track_id = :tid)
            WHEN MATCHED THEN UPDATE SET t.bpm=:bpm, t.music_key=:mkey, t.duration=:dur, t.image_url=:img
            WHEN NOT MATCHED THEN INSERT (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, music_key, duration)
            VALUES (:tid, :title, :artist, :aid, :prev, :img, :bpm, :mkey, :dur)
        """, {'tid': track_id, 'title': title, 'artist': artist, 'aid': album_id, 'prev': preview, 'img': image_url, 'bpm': bpm, 'mkey': music_key, 'dur': duration_iso})

        tags = set(["tag:Spotify"])
        if genres: tags.add("tag:MovieOST")
        
        energy = a_data.get('energy', 0)
        valence = a_data.get('valence', 0)
        if energy > 0.7: tags.add('tag:Exciting')
        if energy < 0.4: tags.add('tag:Rest')
        if valence < 0.3: tags.add('tag:Sentimental')
        if valence > 0.7: tags.add('tag:Pop')

        genre_map = {"액션":"tag:Action", "SF":"tag:SF", "코미디":"tag:Exciting", "드라마":"tag:Sentimental", "멜로":"tag:Romance", "로맨스":"tag:Romance", "공포":"tag:Tension", "호러":"tag:Tension", "스릴러":"tag:Tension", "범죄":"tag:Tension", "애니메이션":"tag:Animation", "가족":"tag:Rest", "뮤지컬":"tag:Pop"}
        for g in genres:
            for k, v in genre_map.items():
                if k in g: tags.add(v)

        for tag in tags:
            try:
                cursor.execute("MERGE INTO TRACK_TAGS USING dual ON (track_id = :tid AND tag_id = :tag) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (:tid, :tag)", {'tid': track_id, 'tag': tag})
            except: pass
            
        cursor.connection.commit()
        return t_data
    except Exception as e:
        print(f"⚠️ 트랙 저장 중 오류: {e}")
        return None

# --- 6. 메인 업데이트 로직 ---
def update_box_office_data():
    print("[Batch] 박스오피스 업데이트 시작...")
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        headers = get_spotify_headers()

        target_dt = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        res = requests.get(KOBIS_BOXOFFICE_URL, params={"key": KOBIS_API_KEY, "targetDt": target_dt, "itemPerPage": "10"}).json()
        movie_list = res.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])

        if not movie_list: return "박스오피스 데이터 없음"

        for movie in movie_list:
            rank = int(movie['rank'])
            title = movie['movieNm']
            print(f"\n[Rank {rank}] {title} 처리 중...")

            # 원제/영문 제목까지 조회
            genres, title_en, title_og = get_kobis_metadata(title)

            poster_url = None
            try:
                tmdb_res = requests.get("https://api.themoviedb.org/3/search/movie", params={"api_key": TMDB_API_KEY, "query": title, "language": "ko-KR"}).json()
                if tmdb_res.get('results'):
                    poster_url = f"https://image.tmdb.org/t/p/w500{tmdb_res['results'][0]['poster_path']}"
            except: pass

            try:
                cursor.execute("""
                    MERGE INTO MOVIES m USING (SELECT :mid AS mid FROM dual) d
                    ON (m.movie_id = d.mid)
                    WHEN MATCHED THEN UPDATE SET rank = :rank, poster_url = :poster
                    WHEN NOT MATCHED THEN INSERT (movie_id, title, rank, poster_url) VALUES (:mid, :title, :rank, :poster)
                """, {'mid': title, 'title': title, 'rank': rank, 'poster': poster_url})
                conn.commit()
            except: pass

            # [수정] 3단계 검색 (원제 -> 영문 -> 한글)
            matched_track = find_best_track([title_og, title_en, title], headers)
            if matched_track:
                track_id = matched_track['id']
                save_track_details(track_id, cursor, headers, genres)
                try:
                    # [핵심] 기존 연결 삭제 후 재연결 (덮어쓰기)
                    cursor.execute("DELETE FROM MOVIE_OSTS WHERE movie_id = :mid", {'mid': title})
                    cursor.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:mid, :tid)", {'mid': title, 'tid': track_id})
                    conn.commit()
                except Exception as e: print(f"    ❌ 연결 저장 실패: {e}")
            else:
                print("    ❌ OST 미발견")

        print("\n[Batch] 업데이트 완료")
        return f"{len(movie_list)}개 영화 업데이트 완료"
    except Exception as e:
        print(f"[Batch 오류] {e}")
        return f"업데이트 실패: {e}"

# --- 7. API 라우트 ---
@app.route('/api/spotify-token', methods=['GET'])
def api_get_token():
    try:
        headers = get_spotify_headers()
        token = headers['Authorization'].split(' ')[1]
        return jsonify({"access_token": token})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/search', methods=['GET'])
def api_search():
    query = request.args.get('q', '')
    type_ = request.args.get('type', 'track')
    if not query: return jsonify({"error": "검색어 필요"}), 400
    try:
        headers = get_spotify_headers()
        res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params={"q": query, "type": type_, "limit": 20, "market": "KR"})
        return jsonify(res.json()), res.status_code
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/track/<track_id>', methods=['GET'])
def api_get_track_detail(track_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT track_title, artist_name, image_url, bpm, music_key, duration FROM TRACKS WHERE track_id = :tid", {'tid': track_id})
        row = cursor.fetchone()
        if row and row[3]:
            return jsonify({"id": track_id, "title": row[0], "artist": row[1], "image": row[2], "bpm": row[3], "key": row[4], "duration": row[5], "source": "DB"})
        
        headers = get_spotify_headers()
        res = save_track_details(track_id, cursor, headers, genres=[])
        if not res: return jsonify({"error": "Track not found"}), 404
        
        cursor.execute("SELECT track_title, artist_name, image_url, bpm, music_key, duration FROM TRACKS WHERE track_id = :tid", {'tid': track_id})
        new_row = cursor.fetchone()
        return jsonify({"id": track_id, "title": new_row[0], "artist": new_row[1], "image": new_row[2], "bpm": new_row[3], "key": new_row[4], "duration": new_row[5], "source": "Spotify->DB"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/movie/<movie_id>/update-ost', methods=['POST'])
def api_update_movie_ost(movie_id):
    data = request.json
    spotify_url = data.get('spotifyUrl')
    user_ip = request.remote_addr 
    if not spotify_url: return jsonify({"error": "Link required"}), 400

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        headers = get_spotify_headers()

        # ID 디코딩 (Base64 -> 한글 제목)
        real_movie_id = movie_id
        try:
            if movie_id.endswith('_ost'): movie_id = movie_id[:-4]
            padding = len(movie_id) % 4
            if padding: movie_id += '=' * (4 - padding)
            decoded = base64.urlsafe_b64decode(movie_id).decode('utf-8')
            # DB 검증
            cursor.execute("SELECT count(*) FROM MOVIES WHERE movie_id = :mid", {'mid': decoded})
            if cursor.fetchone()[0] > 0: real_movie_id = decoded
        except: pass

        track_id = extract_spotify_id(spotify_url)
        if not track_id: return jsonify({"error": "Invalid Link"}), 400

        result = save_track_details(track_id, cursor, headers, genres=[])
        if not result: return jsonify({"error": "Track not found"}), 404

        cursor.execute("SELECT track_id FROM MOVIE_OSTS WHERE movie_id = :mid", {'mid': real_movie_id})
        prev_row = cursor.fetchone()
        prev_id = prev_row[0] if prev_row else "NONE"

        cursor.execute("DELETE FROM MOVIE_OSTS WHERE movie_id = :mid", {'mid': real_movie_id})
        cursor.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:mid, :tid)", {'mid': real_movie_id, 'tid': track_id})

        cursor.execute("""
            INSERT INTO MODIFICATION_LOGS (target_type, target_id, action_type, previous_value, new_value, user_ip)
            VALUES (:type, :tgt, 'UPDATE', :prev, :new, :ip)
        """, {'type': 'MOVIE_OST', 'tgt': real_movie_id, 'prev': prev_id, 'new': track_id, 'ip': user_ip})

        conn.commit()
        return jsonify({"message": "Updated", "new_track": result['name']})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/admin/update-movies', methods=['POST'])
def api_update_movies():
    msg = update_box_office_data()
    return jsonify({"message": msg})

@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_box_office_ttl():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        query = """
            SELECT m.movie_id, m.title, m.rank, m.poster_url, t.track_id, t.track_title, t.artist_name, t.preview_url, a.album_cover_url
            FROM MOVIES m
            LEFT JOIN MOVIE_OSTS mo ON m.movie_id = mo.movie_id
            LEFT JOIN TRACKS t ON mo.track_id = t.track_id
            LEFT JOIN ALBUMS a ON t.album_id = a.album_id
            WHERE m.rank <= 10 ORDER BY m.rank ASC
        """
        cursor.execute(query)
        rows = cursor.fetchall()
        
        ttl = """@prefix schema: <http://schema.org/> .\n@prefix komc: <https://knowledgemap.kr/komc/def/> .\n@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .\n"""
        tag_cursor = conn.cursor()

        for row in rows:
            mid, mtitle, rank, mposter, tid, ttitle, artist, preview, cover = row
            m_uri = base64.urlsafe_b64encode(mid.encode()).decode().rstrip("=")
            mposter = mposter or "img/playlist-placeholder.png"
            ttitle = ttitle or "OST 정보 없음"
            artist = artist or "-"
            cover = cover or "img/playlist-placeholder.png"
            preview = preview or ""

            tags_str = ""
            if tid:
                try:
                    tag_cursor.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id = :tid", {'tid': tid})
                    tags = [t[0].replace('tag:', '') for t in tag_cursor.fetchall()]
                    if tags: tags_str = f"    komc:relatedTag tag:{', tag:'.join(tags)} ;"
                except: pass

            ttl += f"""
<https://knowledgemap.kr/komc/resource/movie/{m_uri}> a schema:Movie ;
    schema:name "{mtitle}" ; schema:image "{mposter}" ; komc:rank {rank} .
<https://knowledgemap.kr/komc/resource/track/{m_uri}_ost> a schema:MusicRecording ;
    schema:name "{ttitle}" ; schema:byArtist "{artist}" ; schema:image "{cover}" ;
    schema:audio "{preview}" ; komc:featuredIn <https://knowledgemap.kr/komc/resource/movie/{m_uri}> ;
{tags_str}
    schema:genre "Movie Soundtrack" .
"""
        return Response(ttl, mimetype='text/turtle')
    except Exception as e: return f"# Error: {e}", 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)