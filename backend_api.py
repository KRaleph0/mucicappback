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

SPOTIFY_auth_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

# KOBIS URL (상세 정보 API 추가됨)
KOBIS_BOXOFFICE_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/boxoffice/searchDailyBoxOfficeList.json"
KOBIS_MOVIE_INFO_URL = "http://www.kobis.or.kr/kobisopenapi/webservice/rest/movie/searchMovieInfo.json"

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

# --- 2. 헬퍼 함수 (유사도 검사 & 정제) ---
def clean_text(text):
    """OST, Soundtrack 등 불필요한 단어를 제거하고 소문자 변환"""
    if not text: return ""
    text = text.lower()
    # 제거할 키워드 패턴 (괄호 포함)
    patterns = [
        r'\(.*?ost.*?\)', r'original motion picture soundtrack', 
        r'soundtrack', r'ost', r'music from the motion picture'
    ]
    for pat in patterns:
        text = re.sub(pat, '', text)
    # 특수문자 제거 및 공백 정리
    text = re.sub(r'[^a-z0-9가-힣\s]', ' ', text)
    return ' '.join(text.split())

def get_similarity(a, b):
    """두 문자열의 유사도 반환 (0.0 ~ 1.0)"""
    return SequenceMatcher(None, clean_text(a), clean_text(b)).ratio()

def get_spotify_headers():
    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    res = requests.post(SPOTIFY_auth_URL, 
                        headers={'Authorization': f'Basic {b64_auth}', 'Content-Type': 'application/x-www-form-urlencoded'}, 
                        data={'grant_type': 'client_credentials'})
    if res.status_code != 200: raise Exception("Spotify Auth Failed")
    return {'Authorization': f'Bearer {res.json().get("access_token")}'}

def ms_to_iso_duration(ms):
    if not ms: return "PT0M0S"
    seconds = int((ms / 1000) % 60)
    minutes = int((ms / (1000 * 60)) % 60)
    return f"PT{minutes}M{seconds}S"

# --- 3. KOBIS 상세 정보 조회 (장르 + 원제/영문) ---
def get_movie_detail_info(movie_cd):
    """영화 코드로 상세 정보를 조회 (원제, 영문제목, 장르)"""
    params = {'key': KOBIS_API_KEY, 'movieCd': movie_cd}
    try:
        res = requests.get(KOBIS_MOVIE_INFO_URL, params=params).json()
        info = res.get('movieInfoResult', {}).get('movieInfo', {})
        
        if not info: return None, None, []

        title_en = info.get('movieNmEn', '')
        title_og = info.get('movieNmOg', '') # 원제 (일본어 등)
        
        genres = [g['genreNm'] for g in info.get('genres', [])]
        
        print(f"    ℹ️ 상세 정보: En='{title_en}', Og='{title_og}', 장르={genres}")
        return title_en, title_og, genres
    except Exception as e:
        print(f"    ⚠️ 상세 조회 실패: {e}")
        return None, None, []

# --- 4. Spotify 검색 및 매칭 (업그레이드됨) ---
def find_best_track(titles, headers):
    """여러 제목(원제, 영문, 한글)으로 검색하고 유사도 0.5 이상인 곡을 찾음"""
    
    # 중복 제거 및 유효한 제목만 필터링
    search_candidates = []
    seen = set()
    for t in titles:
        if t and t not in seen:
            search_candidates.append(t)
            seen.add(t)

    for title in search_candidates:
        query = f"{title} ost"
        print(f"    🎵 검색 시도: '{query}'")
        
        params = {"q": query, "type": "track", "limit": 5, "market": "KR"} # 상위 5개 확인
        try:
            res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params).json()
            tracks = res.get('tracks', {}).get('items', [])
            
            for track in tracks:
                track_name = track['name']
                album_name = track['album']['name']
                
                # 유사도 검사 (곡 제목 또는 앨범 제목과 비교)
                sim_track = get_similarity(title, track_name)
                sim_album = get_similarity(title, album_name)
                
                # [조건] 유사도가 0.5(50%) 이상이어야 통과
                if sim_track >= 0.5 or sim_album >= 0.5:
                    print(f"      ✅ 매칭 성공! (유사도: {max(sim_track, sim_album):.2f}) - {track_name}")
                    return track
                else:
                    # print(f"      ❌ 유사도 미달 ({sim_track:.2f}/{sim_album:.2f}): {track_name}")
                    pass
                    
        except Exception as e:
            print(f"      ⚠️ 검색 에러: {e}")

    print("    ❌ 모든 검색 시도 실패 (유사한 곡 없음)")
    return None

# --- 5. 트랙 저장 및 태깅 ---
def save_track_details(track, cursor, headers, genres=[]):
    try:
        track_id = track['id']
        
        # 상세 특징(BPM 등) 추가 조회
        audio_res = requests.get(f"{SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers)
        a_data = audio_res.json() if audio_res.status_code == 200 else {}

        # 데이터 파싱
        title = track.get('name', 'Unknown')
        artist = track['artists'][0]['name'] if track.get('artists') else 'Unknown'
        preview = track.get('preview_url', '')
        album_id = track.get('album', {}).get('id')
        image_url = track.get('album', {}).get('images', [{}])[0].get('url', '')
        
        bpm = a_data.get('tempo', 0)
        key_int = a_data.get('key', -1)
        music_key = PITCH_CLASS[key_int] if 0 <= key_int < len(PITCH_CLASS) else 'Unknown'
        duration_iso = ms_to_iso_duration(track.get('duration_ms', 0))

        # DB 저장 (앨범 -> 트랙 -> 태그)
        if album_id:
            cursor.execute("MERGE INTO ALBUMS USING dual ON (album_id = :aid) WHEN NOT MATCHED THEN INSERT (album_id, album_cover_url) VALUES (:aid, :cover)", {'aid': album_id, 'cover': image_url})

        cursor.execute("""
            MERGE INTO TRACKS t USING dual ON (t.track_id = :tid)
            WHEN MATCHED THEN 
                UPDATE SET t.bpm = :bpm, t.music_key = :mkey, t.duration = :dur, t.image_url = :img
            WHEN NOT MATCHED THEN 
                INSERT (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, music_key, duration)
                VALUES (:tid, :title, :artist, :aid, :prev, :img, :bpm, :mkey, :dur)
        """, {'tid': track_id, 'title': title, 'artist': artist, 'aid': album_id, 'prev': preview, 'img': image_url, 'bpm': bpm, 'mkey': music_key, 'dur': duration_iso})

        # 태그 매핑
        tags = set(["tag:Spotify"])
        if genres: tags.add("tag:MovieOST")
        
        # 오디오 특징 태그
        energy = a_data.get('energy', 0)
        valence = a_data.get('valence', 0)
        if energy > 0.7: tags.add('tag:Exciting')
        if energy < 0.4: tags.add('tag:Rest')
        if valence < 0.3: tags.add('tag:Sentimental')
        if valence > 0.7: tags.add('tag:Pop')

        # 장르 태그 (부분 일치)
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
                cursor.execute("MERGE INTO TRACK_TAGS USING dual ON (track_id = :tid AND tag_id = :tag) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (:tid, :tag)", {'tid': track_id, 'tag': tag})
            except: pass
            
        cursor.connection.commit()
        return track

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
            movie_cd = movie['movieCd'] # 영화 코드
            print(f"\n[Rank {rank}] {title} (Code: {movie_cd}) 처리 중...")

            # 1. 상세 정보 조회 (원제, 영문, 장르)
            title_en, title_og, genres = get_movie_detail_info(movie_cd)

            # 2. TMDB 포스터 (제목 사용)
            poster_url = None
            try:
                tmdb_res = requests.get("https://api.themoviedb.org/3/search/movie", 
                                      params={"api_key": TMDB_API_KEY, "query": title, "language": "ko-KR"}).json()
                if tmdb_res.get('results'):
                    poster_url = f"https://image.tmdb.org/t/p/w500{tmdb_res['results'][0]['poster_path']}"
            except: pass

            # 3. Spotify 검색 및 매칭 (3단계: 원제 -> 영문 -> 한글)
            # 순서: [원제, 영문제목, 한글제목]
            search_titles = [title_og, title_en, title]
            matched_track = find_best_track(search_titles, headers)
            
            track_id = None
            if matched_track:
                track_id = matched_track['id']
                save_track_details(matched_track, cursor, headers, genres)

            # 4. 영화 정보 저장
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
        print(f"[Batch 오류] {e}")
        return f"업데이트 실패: {e}"

# --- 7. API 라우트 (기존 유지) ---
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
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT track_title, artist_name, image_url, bpm, music_key, duration FROM TRACKS WHERE track_id = :tid", {'tid': track_id})
        row = cursor.fetchone()
        
        if row and row[3]:
            return jsonify({
                "id": track_id, "title": row[0], "artist": row[1], "image": row[2], "bpm": row[3], "key": row[4], "duration": row[5], "source": "DB"
            })
        
        headers = get_spotify_headers()
        # 임시 트랙 정보 조회
        t_data = requests.get(f"{SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers).json()
        save_track_details(t_data, cursor, headers, genres=[])
        
        cursor.execute("SELECT track_title, artist_name, image_url, bpm, music_key, duration FROM TRACKS WHERE track_id = :tid", {'tid': track_id})
        new_row = cursor.fetchone()
        if new_row:
            return jsonify({
                "id": track_id, "title": new_row[0], "artist": new_row[1], "image": new_row[2], "bpm": new_row[3], "key": new_row[4], "duration": new_row[5], "source": "Spotify->DB"
            })
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
        
        ttl = """@prefix schema: <http://schema.org/> .\n@prefix komc: <https://knowledgemap.kr/komc/def/> .\n"""
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
    except Exception as e:
        return f"# Error: {e}", 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)