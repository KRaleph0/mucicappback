import os
import requests
import base64
import oracledb 
from flask import Flask, request, jsonify, g
from flask_cors import CORS # ❗️ [신규] CORS 라이브러리 임포트
from datetime import datetime

# --- 1. 설정 (Spotify + Oracle DB) ---
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "f31f9f9e292a47f6b687645f25cfdb19")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "7b287aa77a51486ba95544983f5d7a63")
SPOTIFY_AUTH_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

# ❗️ [🛑 수정] Oracle DB 연결 정보
DB_USER = os.getenv("DB_USER", "YOUR_ORACLE_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD", "YOUR_ORACLE_PASSWORD")
# 요청하신 'ordb.mirinea.org'를 호스트로 사용
DB_HOST = "ordb.mirinea.org" 
DB_PORT = os.getenv("DB_PORT", "1521") # 기본 Oracle 포트
DB_SERVICE_NAME = os.getenv("DB_SERVICE_NAME", "YOUR_SERVICE_NAME") # 예: XEPDB1

# DSN (Data Source Name) 조합
DB_DSN = f"{DB_HOST}:{DB_PORT}/{DB_SERVICE_NAME}"
print(f"[DB] 연결 시도: {DB_DSN}")

# --- 2. Flask 앱 및 DB 연결 설정 ---
app = Flask(__name__)
# [❗️ 신규] CORS 설정 추가 (모든 출처에서 /api/ 경로 허용)
CORS(app, resources={r"/api/*": {"origins": "*"}}) 

try:
    db_pool = oracledb.create_pool(user=DB_USER, password=DB_PASSWORD, dsn=DB_DSN, min=1, max=5)
    print(f"[DB] Oracle Pool 생성 완료.")
except Exception as e:
    print(f"[DB 오류] Oracle Pool 생성 실패: {e}")
    db_pool = None # 풀 생성 실패 시 None으로 설정

def get_db_connection():
    """DB 커넥션 풀에서 연결 가져오기"""
    if not db_pool:
        raise Exception("DB 풀이 초기화되지 않았습니다. DSN 정보를 확인하세요.")
    if 'db' not in g:
        g.db = db_pool.acquire()
    return g.db

@app.teardown_appcontext
def close_db_connection(exception):
    """요청 종료 시 DB 연결 반환"""
    db = g.pop('db', None)
    if db is not None:
        db.release()

# --- 3. Spotify API 헬퍼 ---
@app.route('/api/spotify-token', methods=['GET'])
def get_spotify_token():
    # 키를 코드에 하드코딩하지 않고, 환경 변수에서 안전하게 불러옵니다.
    client_id = os.environ.get('SPOTIFY_CLIENT_ID')
    client_secret = os.environ.get('SPOTIFY_CLIENT_SECRET')

    if not client_id or not client_secret:
        return jsonify({"error": "Spotify API 키가 서버에 설정되지 않았습니다."}), 500

    # 스포티파이에 토큰 요청
    auth_url = 'https://accounts.spotify.com/api/token'
    auth_header = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    
    try:
        response = requests.post(
            auth_url,
            headers={
                'Authorization': f'Basic {auth_header}',
                'Content-Type': 'application/x-www-form-urlencoded'
            },
            data={'grant_type': 'client_credentials'}
        )
        
        response.raise_for_status() # 오류가 있으면 예외 발생
        token_data = response.json()
        
        # 프론트엔드에는 'access_token'만 전달
        return jsonify({"access_token": token_data.get("access_token")})

    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"스포티파이 토큰 요청 실패: {str(e)}"}), 502

def get_spotify_headers():
    token = get_spotify_token()
    return {'Authorization': f'Bearer {token}'}

KEY_MAP = {0: "C", 1: "C#", 2: "D", 3: "D#", 4: "E", 5: "F", 6: "F#", 7: "G", 8: "G#", 9: "A", 10: "A#", 11: "B"}

# --- 4. DB 확인 및 생성 로직 (핵심) ---
def db_check_or_create_track(track_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # 1. DB에서 트랙 확인
        cursor.execute("SELECT track_id FROM TRACKS WHERE track_id = :1", [track_id])
        if cursor.fetchone():
            return "이미 존재함"

        print(f"[DB] 트랙 {track_id} 없음. Spotify에서 정보 가져오기...")
        
        # 2. DB에 없으면 Spotify API 2개 동시 호출
        headers = get_spotify_headers()
        track_res = requests.get(f"{SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers, params={"market": "KR"})
        track_res.raise_for_status()
        track_data = track_res.json()
        
        features_res = requests.get(f"{SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers)
        features_res.raise_for_status()
        features_data = features_res.json()

        # 3. 데이터 정제 (스키마 매핑)
        album_data = track_data.get('album', {})
        artist_data_list = track_data.get('artists', [])
        album_id = album_data.get('id')
        
        album_payload = {
            "album_id": album_id,
            "album_title": album_data.get('name'),
            "album_cover_url": album_data.get('images', [{}])[0].get('url') if album_data.get('images') else None
        }
        
        artists_payload = []
        for artist in artist_data_list:
            artists_payload.append({
                "artist_id": artist.get('id'),
                "artist_name": artist.get('name'),
                "image_url": None 
            })

        track_payload = {
            "track_id": track_id,
            "album_id": album_id,
            "track_title": track_data.get('name'),
            "duration_ms": track_data.get('duration_ms'),
            "preview_url": track_data.get('preview_url'),
            "tempo": features_data.get('tempo'),
            "music_key": KEY_MAP.get(features_data.get('key'), 'N/A'),
            "time_signature": f"{features_data.get('time_signature')}/4",
            "acousticness": features_data.get('acousticness'),
            "danceability": features_data.get('danceability'),
            "energy": features_data.get('energy'),
            "instrumentalness": features_data.get('instrumentalness'),
            "liveness": features_data.get('liveness'),
            "loudness": features_data.get('loudness'),
            "valence": features_data.get('valence'),
            "external_url": track_data.get('external_urls', {}).get('spotify')
        }

        # 4. DB에 삽입 (Transaction)
        cursor.execute("""
            MERGE INTO ALBUMS a
            USING (SELECT :album_id AS album_id FROM dual) d
            ON (a.album_id = d.album_id)
            WHEN NOT MATCHED THEN
              INSERT (album_id, album_title, album_cover_url)
              VALUES (:album_id, :album_title, :album_cover_url)
        """, album_payload)
        
        for artist_payload in artists_payload:
            cursor.execute("""
                MERGE INTO ARTISTS ar
                USING (SELECT :artist_id AS artist_id FROM dual) d
                ON (ar.artist_id = d.artist_id)
                WHEN NOT MATCHED THEN
                  INSERT (artist_id, artist_name, image_url)
                  VALUES (:artist_id, :artist_name, :image_url)
            """, artist_payload)

        cursor.execute("""
            INSERT INTO TRACKS (
                track_id, album_id, track_title, duration_ms, preview_url, 
                tempo, music_key, time_signature, acousticness, danceability, 
                energy, instrumentalness, liveness, loudness, valence,
                external_url
            ) VALUES (
                :track_id, :album_id, :track_title, :duration_ms, :preview_url, 
                :tempo, :music_key, :time_signature, :acousticness, :danceability, 
                :energy, :instrumentalness, :liveness, :loudness, :valence,
                :external_url
            )
        """, track_payload)
        
        for artist_payload in artists_payload:
             cursor.execute("""
                INSERT INTO ARTIST_TRACKS (artist_id, track_id)
                VALUES (:artist_id, :track_id)
            """, {"artist_id": artist_payload["artist_id"], "track_id": track_id})

        conn.commit()
        return "신규 생성됨"

    except Exception as e:
        conn.rollback()
        print(f"[DB 오류] 롤백 실행: {e}")
        raise e 

# --- 5. Flask API 라우트 정의 ---

@app.route("/api/get-or-create-track", methods=['POST'])
def api_get_or_create_track():
    """(1) `search.js`에서 호출 (DB 확인/생성)"""
    try:
        data = request.get_json()
        track_id = data.get('trackId')
        if not track_id:
            return jsonify({"error": "trackId가 필요합니다."}), 400
        message = db_check_or_create_track(track_id)
        return jsonify({"message": message, "trackId": track_id}), 200
    except Exception as e:
        return jsonify({"error": f"서버 오류: {e}"}), 500

@app.route("/api/track-details", methods=['GET'])
def api_get_track_details():
    """(2) `search.js`에서 호출 (상세 정보 조회)"""
    track_id = request.args.get('id')
    if not track_id:
        return jsonify({"error": "id 쿼리 파라미터가 필요합니다."}), 400
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT 
                T.*, 
                A.album_title, 
                A.album_cover_url 
            FROM TRACKS T
            JOIN ALBUMS A ON T.album_id = A.album_id
            WHERE T.track_id = :1
        """, [track_id])
        
        columns = [col[0].lower() for col in cursor.description]
        track_data = cursor.fetchone()
        
        if not track_data:
            return jsonify({"error": "트랙을 찾을 수 없습니다."}), 404
            
        track_dict = dict(zip(columns, track_data))
        
        cursor.execute("""
            SELECT A.artist_name 
            FROM ARTISTS A
            JOIN ARTIST_TRACKS AT ON A.artist_id = AT.artist_id
            WHERE AT.track_id = :1
        """, [track_id])
        
        artists = cursor.fetchall()
        track_dict['artists'] = [artist[0] for artist in artists]
        
        return jsonify(track_dict), 200
    except Exception as e:
        return jsonify({"error": f"DB 조회 오류: {e}"}), 500

# --- 6. 서버 실행 ---
if __name__ == '__main__':
    app.run(debug=True, port=5000)