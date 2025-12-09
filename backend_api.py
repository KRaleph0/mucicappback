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

# 음악 Key 매핑
PITCH_CLASS = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

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

# --- 2. 헬퍼 함수 ---
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

def ms_to_iso_duration(ms):
    """밀리초(ms)를 ISO 8601 형식(PT3M30S)으로 변환"""
    if not ms: return "PT0M0S"
    seconds = int((ms / 1000) % 60)
    minutes = int((ms / (1000 * 60)) % 60)
    return f"PT{minutes}M{seconds}S"

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
        
        print(f"    ⚠️ 장르 정보 없음: {movie_name}")
        return []
    except Exception as e:
        print(f"    ⚠️ 장르 조회 에러 ({movie_name}): {e}")
        return []

# --- 4. 트랙 상세 정보 저장 (BPM, Key, 태그 포함) ---
def save_track_details(track_id, cursor, headers, genres=[]):
    """트랙 상세 정보(BPM 등)를 Spotify에서 가져와 DB에 저장/업데이트"""
    try:
        # Spotify API 호출
        track_res = requests.get(f"{SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers)
        audio_res = requests.get(f"{SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers)
        
        if track_res.status_code != 200: return None
        
        t_data = track_res.json()
        a_data = audio_res.json() if audio_res.status_code == 200 else {}

        # 데이터 파싱
        title = t_data.get('name', 'Unknown')
        artist = t_data['artists'][0]['name'] if t_data.get('artists') else 'Unknown'
        preview = t_data.get('preview_url', '')
        album_id = t_data.get('album', {}).get('id')
        image_url = t_data.get('album', {}).get('images', [{}])[0].get('url', '')
        
        bpm = a_data.get('tempo', 0)
        key_int = a_data.get('key', -1)
        music_key = PITCH_CLASS[key_int] if 0 <= key_int < len(PITCH_CLASS) else 'Unknown'
        duration_ms = t_data.get('duration_ms', 0)
        duration_iso = ms_to_iso_duration(duration_ms)

        # 앨범 저장
        if album_id:
            cursor.execute("""
                MERGE INTO ALBUMS USING dual ON (album_id = :aid) 
                WHEN NOT MATCHED THEN INSERT (album_id, album_cover_url) VALUES (:aid, :cover)
            """, {'aid': album_id, 'cover': image_url})

        # 트랙 저장 (MERGE)
        cursor.execute("""
            MERGE INTO TRACKS t USING dual ON (t.track_id = :tid)
            WHEN MATCHED THEN 
                UPDATE SET t.bpm = :bpm, t.music_key = :mkey, t.duration = :dur, t.image_url = :img
            WHEN NOT MATCHED THEN 
                INSERT (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, music_key, duration)
                VALUES (:tid, :title, :artist, :aid, :prev, :img, :bpm, :mkey, :dur)
        """, {
            'tid': track_id, 'title': title, 'artist': artist, 'aid': album_id,
            'prev': preview, 'img': image_url, 'bpm': bpm, 'mkey': music_key, 'dur': duration_iso
        })

        # 태그 매핑 및 저장
        tags = set(["tag:Spotify"])
        if genres: tags.add("tag:MovieOST")
        
        # 오디오 특징 태그
        energy = a_data.get('energy', 0)
        valence = a_data.get('valence', 0)
        if energy > 0.7: tags.add('tag:Exciting')
        if energy < 0.4: tags.add('tag:Rest')
        if valence < 0.3: tags.add('tag:Sentimental')
        if valence > 0.7: tags.add('tag:Pop')

        # 장르 태그
        genre_map = {
            "액션": "tag:Action", "SF": "tag:SF", "코미디": "tag:Exciting",
            "드라마": "tag:Sentimental", "멜로": "tag:Romance", "로맨스": "tag:Romance",
            "공포": "tag:Tension", "호러": "tag:Tension", "스릴러": "tag:Tension",
            "범죄": "tag:Tension", "애니메이션": "tag:Animation",
            "가족": "tag:Rest", "뮤지컬": "tag:Pop"
        }
        for g in genres:
            for k, v in genre_map.items():
                if k in g: tags.add(v)

        for tag in tags:
            try:
                cursor.execute("""
                    MERGE INTO TRACK_TAGS USING dual ON (track_id = :tid AND tag_id = :tag) 
                    WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (:tid, :tag)
                """, {'tid': track_id, 'tag': tag})
            except: pass
            
        cursor.connection.commit()
        return t_data

    except Exception as e:
        print(f"⚠️ 트랙 저장 중 오류: {e}")
        return None

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

        if not movie_list:
            return "박스오피스 데이터 없음"

        for movie in movie_list:
            rank = int(movie['rank'])
            title = movie['movieNm']
            print(f"\n[Rank {rank}] {title} 처리 중...")

            genres = get_movie_genre(title)
            
            # TMDB 포스터
            poster_url = None
            try:
                tmdb_res = requests.get("https://api.themoviedb.org/3/search/movie", 
                                      params={"api_key": TMDB_API_KEY, "query": title, "language": "ko-KR"}).json()
                if tmdb_res.get('results'):
                    m_data = tmdb_res['results'][0]
                    if m_data.get('poster_path'):
                        poster_url = f"https://image.tmdb.org/t/p/w500{m_data['poster_path']}"
            except: pass

            # Spotify OST 검색 및 저장
            search_query = f"{title} ost"
            params = {"q": search_query, "type": "track", "limit": 1, "market": "KR"}
            track_id = None
            try:
                sp_res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params).json()
                tracks = sp_res.get('tracks', {}).get('items', [])
                if tracks:
                    track_id = tracks[0]['id']
                    # [핵심] 여기서 상세 정보와 태그까지 한 번에 저장
                    save_track_details(track_id, cursor, headers, genres)
            except Exception as e:
                print(f"    ⚠️ Spotify 검색 오류: {e}")

            # 영화 정보 저장
            try:
                cursor.execute("""
                    MERGE INTO MOVIES m USING (SELECT :mid AS mid FROM dual) d
                    ON (m.movie_id = d.mid)
                    WHEN MATCHED THEN UPDATE SET rank = :rank, poster_url = :poster
                    WHEN NOT MATCHED THEN INSERT (movie_id, title, rank, poster_url) VALUES (:mid, :title, :rank, :poster)
                """, {'mid': title, 'title': title, 'rank': rank, 'poster': poster_url})

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

@app.route('/api/track/<track_id>', methods=['GET'])
def api_get_track_detail(track_id):
    """트랙 클릭 시 상세 정보 반환 (Lazy Loading)"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. DB 조회
        cursor.execute("""
            SELECT track_title, artist_name, image_url, bpm, music_key, duration 
            FROM TRACKS WHERE track_id = :tid
        """, {'tid': track_id})
        row = cursor.fetchone()
        
        if row and row[3]: # BPM 정보가 있으면 DB 반환
            return jsonify({
                "id": track_id, "title": row[0], "artist": row[1], 
                "image": row[2], "bpm": row[3], "key": row[4], "duration": row[5],
                "source": "DB"
            })
        
        # 2. 없으면 Spotify 조회 및 저장
        headers = get_spotify_headers()
        save_track_details(track_id, cursor, headers, genres=[])
        
        # 3. 재조회
        cursor.execute("SELECT track_title, artist_name, image_url, bpm, music_key, duration FROM TRACKS WHERE track_id = :tid", {'tid': track_id})
        new_row = cursor.fetchone()
        
        if new_row:
            return jsonify({
                "id": track_id, "title": new_row[0], "artist": new_row[1], 
                "image": new_row[2], "bpm": new_row[3], "key": new_row[4], "duration": new_row[5],
                "source": "Spotify->DB"
            })
        else:
            return jsonify({"error": "Track not found"}), 404

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
                t.track_id, t.track_title, t.artist_name, t.preview_url, a.album_cover_url
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
@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .
"""
        tag_cursor = conn.cursor()

        for row in rows:
            mid, mtitle, rank, mposter, tid, ttitle, artist, preview, cover = row
            m_uri = base64.urlsafe_b64encode(mid.encode()).decode().rstrip("=")
            mposter = mposter or "img/playlist-placeholder.png"
            ttitle = ttitle or "OST 정보 없음"
            artist = artist or "-"
            cover = cover or "img/playlist-placeholder.png"
            preview = preview or ""

            # 태그 조회
            tags_str = ""
            if tid:
                try:
                    tag_cursor.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id = :tid", {'tid': tid})
                    tags = [t[0] for t in tag_cursor.fetchall()]
                    if tags:
                        tags_str = f"    komc:relatedTag {', '.join(tags)} ;"
                except: pass

            ttl += f"""
<https://knowledgemap.kr/komc/resource/movie/{m_uri}> a schema:Movie ;
    schema:name "{mtitle}" ; schema:image "{mposter}" ; komc:rank {rank} .

<https://knowledgemap.kr/komc/resource/track/{m_uri}_ost> a schema:MusicRecording ;
    schema:name "{ttitle}" ; 
    schema:byArtist "{artist}" ; 
    schema:image "{cover}" ;
    schema:audio "{preview}" ;
    komc:featuredIn <https://knowledgemap.kr/komc/resource/movie/{m_uri}> ;
{tags_str}
    schema:genre "Movie Soundtrack" .
"""
        tag_cursor.close()
        return Response(ttl, mimetype='text/turtle')
    except Exception as e:
        return f"# Error: {e}", 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)