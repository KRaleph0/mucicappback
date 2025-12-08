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

# [NEW] 음악 Key 매핑 (0 -> C, 1 -> C# ...)
PITCH_CLASS = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

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

# --- 2. 헬퍼 함수 (Spotify 인증 & 변환) ---
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
        raise Exception(f"Spotify Auth Failed: {res.status_code}")
        
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
            return genre_str.split(',') if genre_str else []
        return []
    except: return []

# --- [핵심] 4. 트랙 상세 저장 (BPM, Key 포함) ---
def save_track_details(track_id, cursor, headers, genres=[]):
    """
    트랙 상세 정보(BPM, Key 등)를 가져와 DB에 저장/업데이트함.
    이미 존재하면 패스하지 않고, 부족한 정보(BPM 등)가 있으면 채워넣음.
    """
    try:
        # 1. Spotify 기본 정보 + 오디오 특징 조회
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
        
        # 오디오 특징 파싱
        bpm = a_data.get('tempo', 0)
        key_int = a_data.get('key', -1)
        music_key = PITCH_CLASS[key_int] if 0 <= key_int < len(PITCH_CLASS) else 'Unknown'
        duration_ms = t_data.get('duration_ms', 0)
        duration_iso = ms_to_iso_duration(duration_ms)

        # 2. 앨범 저장 (MERGE)
        if album_id:
            cursor.execute("""
                MERGE INTO ALBUMS USING dual ON (album_id = :aid) 
                WHEN NOT MATCHED THEN INSERT (album_id, album_cover_url) VALUES (:aid, :cover)
            """, {'aid': album_id, 'cover': image_url})

        # 3. 트랙 저장 (MERGE - 기존에 있어도 BPM 등이 비어있으면 업데이트)
        cursor.execute("""
            MERGE INTO TRACKS t USING dual ON (t.track_id = :tid)
            WHEN MATCHED THEN 
                UPDATE SET 
                    t.bpm = :bpm, 
                    t.music_key = :mkey, 
                    t.duration = :dur,
                    t.image_url = :img
            WHEN NOT MATCHED THEN 
                INSERT (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, music_key, duration)
                VALUES (:tid, :title, :artist, :aid, :prev, :img, :bpm, :mkey, :dur)
        """, {
            'tid': track_id, 'title': title, 'artist': artist, 'aid': album_id,
            'prev': preview, 'img': image_url, 'bpm': bpm, 'mkey': music_key, 'dur': duration_iso
        })

        # 4. 태그 저장 (영화 장르 + 오디오 특징 기반 자동 태깅)
        tags = set(["tag:Spotify"])
        if genres: tags.add("tag:MovieOST")
        
        # 오디오 특징 기반 자동 태깅
        energy = a_data.get('energy', 0)
        valence = a_data.get('valence', 0)
        
        if energy > 0.7: tags.add('tag:Exciting')
        if energy < 0.4: tags.add('tag:Rest')
        if valence < 0.3: tags.add('tag:Sentimental')
        if valence > 0.7: tags.add('tag:Pop')

        # 영화 장르 매핑
        genre_map = {"액션":"tag:Action", "로맨스":"tag:Romance", "공포":"tag:Tension"}
        for g in genres:
            for k, v in genre_map.items():
                if k in g: tags.add(v)

        for tag in tags:
            try:
                cursor.execute("MERGE INTO TRACK_TAGS USING dual ON (track_id = :tid AND tag_id = :tag) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (:tid, :tag)", {'tid': track_id, 'tag': tag})
            except: pass
            
        cursor.connection.commit()
        return t_data # 저장된 정보 반환

    except Exception as e:
        print(f"⚠️ 트랙 저장 중 오류: {e}")
        return None

# --- 5. 데이터 업데이트 (배치) ---
def update_box_office_data():
    # (기존 로직 유지하되 save_track_details 호출로 변경)
    # ... (생략: 기존 코드에서 db_save_track_with_genre_tags 호출 부분을 save_track_details로 교체하면 됨)
    # 여기서는 지면 관계상 전체 코드를 다 붙이기보다 핵심만 보여드립니다.
    pass 

# --- 6. API 라우트 ---

# [NEW] 트랙 상세 정보 조회 및 저장 (Lazy Loading)
@app.route('/api/track/<track_id>', methods=['GET'])
def api_get_track_detail(track_id):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # 1. DB에 있는지 확인
        cursor.execute("""
            SELECT track_title, artist_name, image_url, bpm, music_key, duration 
            FROM TRACKS WHERE track_id = :tid
        """, {'tid': track_id})
        row = cursor.fetchone()
        
        if row and row[3]: # BPM까지 이미 데이터가 꽉 차있으면 바로 반환
            return jsonify({
                "id": track_id, "title": row[0], "artist": row[1], 
                "image": row[2], "bpm": row[3], "key": row[4], "duration": row[5],
                "source": "DB"
            })
        
        # 2. 없거나 부족하면 Spotify에서 긁어와서 저장
        headers = get_spotify_headers()
        # 장르 정보는 알 수 없으므로 빈 리스트 전달 (필요시 파라미터로 받기 가능)
        save_track_details(track_id, cursor, headers, genres=[])
        
        # 3. 저장 후 다시 조회해서 반환 (또는 저장된 데이터 바로 가공)
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
        print(f"[Track API Error] {e}")
        return jsonify({"error": str(e)}), 500

# ... (기존 검색, 토큰 API 등 유지) ...

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)