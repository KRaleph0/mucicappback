import os
import requests
import base64
import oracledb
import re
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, g, Response, send_from_directory
from flask_cors import CORS
import uuid
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

# --- 1. 설정 ---
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
# [필수] 정식 Spotify 인증 URL 사용
SPOTIFY_auth_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API_BASE = "https://api.spotify.com/v1"

# Oracle DB 설정
DB_USER = os.getenv("DB_USER", "admin")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
DB_DSN = os.getenv("DB_DSN", "ordb.mirinea.org:1521/XEPDB1")

PITCH_CLASS = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']

# 파일 업로드 폴더 설정
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
CORS(app)

# DB 연결 풀 생성
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
        raise Exception("Spotify API Key 누락")
    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    b64_auth = base64.b64encode(auth_str.encode()).decode()
    headers = {
        'Authorization': f'Basic {b64_auth}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }
    data = {'grant_type': 'client_credentials'}
    res = requests.post(SPOTIFY_auth_URL, headers=headers, data=data)
    if res.status_code != 200: 
        print("Spotify Auth Error:", res.text)
        raise Exception("Spotify Auth Failed")
    return {'Authorization': f'Bearer {res.json().get("access_token")}'}

def ms_to_iso_duration(ms):
    if not ms: return "PT0M0S"
    seconds = int((ms / 1000) % 60)
    minutes = int((ms / (1000 * 60)) % 60)
    return f"PT{minutes}M{seconds}S"

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# --- 3. [핵심] 트랙 자동 등록 및 조회수 증가 (Lazy Loading) ---
def ensure_track_in_db(track_id):
    """
    DB에 트랙이 있으면 조회수를 증가시키고 태그를 반환,
    없으면 Spotify에서 가져와 저장(조회수 1) 후 태그 반환.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    # 1. DB 존재 여부 확인 및 조회수 증가
    cursor.execute("""
        SELECT t.track_title, LISTAGG(tt.tag_id, ', ') WITHIN GROUP (ORDER BY tt.tag_id)
        FROM TRACKS t
        LEFT JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
        WHERE t.track_id = :1
        GROUP BY t.track_title
    """, [track_id])
    
    row = cursor.fetchone()
    if row:
        # [NEW] 조회수 +1 업데이트
        try:
            cursor.execute("UPDATE TRACKS SET view_count = view_count + 1 WHERE track_id = :1", [track_id])
            conn.commit()
            print(f"📈 [View] ID {track_id} 조회수 증가")
        except Exception as e:
            print(f"⚠️ 조회수 업데이트 실패: {e}")

        # 태그 목록 반환
        tags = row[1].split(', ') if row[1] else []
        return {"status": "exists", "tags": [t.replace('tag:', '') for t in tags]}

    # 2. 없으면 Spotify에서 정보 가져오기
    print(f"📥 [New Track] ID {track_id} DB 등록 시작...")
    headers = get_spotify_headers()
    
    try:
        # 트랙 정보
        t_res = requests.get(f"{SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers)
        if t_res.status_code != 200: return None
        t_data = t_res.json()
        
        # 오디오 특징 (태그 생성용)
        a_res = requests.get(f"{SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers)
        a_data = a_res.json() if a_res.status_code == 200 else {}

        # 데이터 정제
        title = t_data.get('name', 'Unknown')
        artist = t_data['artists'][0]['name'] if t_data.get('artists') else 'Unknown'
        album_id = t_data.get('album', {}).get('id')
        album_img = t_data.get('album', {}).get('images', [{}])[0].get('url', '')
        preview_url = t_data.get('preview_url')
        
        bpm = a_data.get('tempo', 0)
        key_int = a_data.get('key', -1)
        music_key = PITCH_CLASS[key_int] if 0 <= key_int < 12 else 'Unknown'
        duration = ms_to_iso_duration(t_data.get('duration_ms', 0))

        # 3. DB 저장 (MERGE) - [NEW] view_count 초기값 1 설정
        if album_id:
            cursor.execute("MERGE INTO ALBUMS USING dual ON (album_id=:1) WHEN NOT MATCHED THEN INSERT (album_id, album_cover_url) VALUES (:1, :2)", [album_id, album_img])
        
        # 트랙 저장 시 view_count를 1로 설정
        cursor.execute("""
            MERGE INTO TRACKS t USING dual ON (t.track_id=:tid)
            WHEN NOT MATCHED THEN INSERT (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, music_key, duration, view_count)
            VALUES (:tid, :title, :artist, :aid, :prev, :img, :bpm, :mkey, :dur, 1)
        """, {'tid':track_id, 'title':title, 'artist':artist, 'aid':album_id, 'prev':preview_url, 'img':album_img, 'bpm':bpm, 'mkey':music_key, 'dur':duration})

        # 4. 자동 태깅 로직
        tags = set(["tag:Spotify"])
        energy = a_data.get('energy', 0)
        valence = a_data.get('valence', 0)
        danceability = a_data.get('danceability', 0)

        if energy > 0.7: tags.add('tag:Exciting')
        if energy < 0.4: tags.add('tag:Rest')
        if valence < 0.3: tags.add('tag:Sentimental')
        if valence > 0.7: tags.add('tag:Happy')
        if danceability > 0.7: tags.add('tag:Dance')
        if 0.4 <= valence <= 0.7: tags.add('tag:Pop')

        for tag in tags:
            try:
                cursor.execute("MERGE INTO TRACK_TAGS USING dual ON (track_id=:1 AND tag_id=:2) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (:1, :2)", [track_id, tag])
            except: pass
        
        conn.commit()
        print(f"✅ [New Track] {title} 등록 완료 (Tags: {tags})")
        
        return {"status": "created", "tags": [t.replace('tag:', '') for t in tags]}

    except Exception as e:
        print(f"❌ 트랙 등록 실패: {e}")
        conn.rollback()
        return None

# --- 4. API 라우트 ---

# [NEW] 트랙 상세 및 등록 API (조회수 증가 포함)
@app.route('/api/track/<track_id>/details', methods=['GET'])
def api_track_details(track_id):
    result = ensure_track_in_db(track_id)
    if result:
        return jsonify(result)
    else:
        return jsonify({"error": "Failed to fetch or save track"}), 500

# [NEW] 검색 API - 조회수 기반 랭킹 적용
@app.route('/api/search', methods=['GET'])
def api_search():
    query = request.args.get('q')
    search_type = request.args.get('type', 'track')
    limit = request.args.get('limit', '20')
    offset = request.args.get('offset', '0')
    
    if not query: return jsonify({"error": "No query"}), 400
    
    try:
        # 1. Spotify 검색 결과 가져오기
        headers = get_spotify_headers()
        params = {"q": query, "type": search_type, "limit": limit, "offset": offset, "market": "KR"}
        res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
        data = res.json()

        # 2. 결과 재정렬 (조회수 기반)
        if 'tracks' in data and 'items' in data['tracks']:
            items = data['tracks']['items']
            if items:
                # 검색된 트랙 ID 추출
                track_ids = [t['id'] for t in items]
                
                # DB에서 해당 ID들의 조회수 조회
                conn = get_db_connection()
                cur = conn.cursor()
                
                # 동적 바인딩 변수 생성 (:id0, :id1 ...)
                bind_names = [f":id{i}" for i in range(len(track_ids))]
                bind_dict = {f"id{i}": tid for i, tid in enumerate(track_ids)}
                
                sql = f"SELECT track_id, view_count FROM TRACKS WHERE track_id IN ({','.join(bind_names)})"
                cur.execute(sql, bind_dict)
                
                # ID:조회수 맵핑 생성 (DB에 없으면 0)
                view_counts = {row[0]: row[1] for row in cur.fetchall()}
                
                # [핵심] 조회수 내림차순 정렬 (조회수가 같으면 원래 순서 유지 - Stable Sort)
                items.sort(key=lambda x: view_counts.get(x['id'], 0), reverse=True)
                
                # 정렬된 리스트로 교체
                data['tracks']['items'] = items
                
                # 디버깅용: 상위 3개 조회수 출력
                top_views = [view_counts.get(t['id'], 0) for t in items[:3]]
                print(f"🔍 검색 '{query}' 재정렬 완료 (Top Views: {top_views})")

        return jsonify(data)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

# [NEW] DB 데이터 기반 동적 TTL 생성 API
@app.route('/api/data/music.ttl', methods=['GET'])
def get_dynamic_ttl():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        # 조회수가 높은 순으로 상위 100개 트랙만 TTL로 생성 (인기 곡 위주 그래프)
        query = """
            SELECT t.track_id, t.track_title, t.artist_name, a.album_cover_url, t.preview_url,
                   LISTAGG(tt.tag_id, ', ') WITHIN GROUP (ORDER BY tt.tag_id) as tags,
                   t.view_count
            FROM TRACKS t
            LEFT JOIN ALBUMS a ON t.album_id = a.album_id
            LEFT JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
            GROUP BY t.track_id, t.track_title, t.artist_name, a.album_cover_url, t.preview_url, t.view_count
            ORDER BY t.view_count DESC
            FETCH FIRST 100 ROWS ONLY
        """
        cur.execute(query)
        rows = cur.fetchall()
        
        ttl = "@prefix schema: <http://schema.org/> .\n"
        ttl += "@prefix komc: <https://knowledgemap.kr/komc/def/> .\n"
        ttl += "@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .\n\n"
        
        for row in rows:
            tid, title, artist, cover, preview, tags_str, views = row
            cover = cover or ""
            preview = preview or ""
            
            ttl += f'komc:{tid} a schema:MusicRecording ;\n'
            ttl += f'    schema:name "{title}" ;\n'
            ttl += f'    schema:byArtist "{artist}" ;\n'
            ttl += f'    schema:image "{cover}" ;\n'
            ttl += f'    komc:viewCount {views} ;\n' # 조회수 정보도 TTL에 포함
            if preview:
                ttl += f'    schema:audio "{preview}" ;\n'
            
            if tags_str:
                tag_list = tags_str.split(', ')
                formatted_tags = ", ".join([t if t.startswith('tag:') else f'tag:{t}' for t in tag_list])
                ttl += f'    komc:relatedTag {formatted_tags} .\n\n'
            else:
                ttl += '    .\n\n'

        return Response(ttl, mimetype='text/turtle')

    except Exception as e:
        print(f"TTL 생성 오류: {e}")
        return jsonify({"error": str(e)}), 500

# [기존] 토큰 발급 (유지)
@app.route('/api/spotify-token', methods=['GET'])
def api_tk():
    try:
        headers = get_spotify_headers()
        token = headers.get('Authorization', '').split(' ')[1]
        return jsonify({'access_token': token})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# [기존] 프로필 이미지 업로드 (유지)
@app.route('/api/user/profile-image', methods=['POST'])
def upload_profile_image():
    if 'file' not in request.files: return jsonify({"error": "No file"}), 400
    file = request.files['file']
    user_id = request.form.get('user_id')
    if file and allowed_file(file.filename) and user_id:
        try:
            ext = file.filename.rsplit('.', 1)[1].lower()
            filename = secure_filename(f"{user_id}_{uuid.uuid4().hex[:8]}.{ext}")
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)
            image_url = f"/uploads/{filename}"
            conn = get_db_connection(); cur = conn.cursor()
            cur.execute("UPDATE USERS SET profile_img = :1 WHERE user_id = :2", [image_url, user_id])
            conn.commit()
            return jsonify({"message": "OK", "image_url": image_url})
        except Exception as e: return jsonify({"error": str(e)}), 500
    return jsonify({"error": "Invalid request"}), 400

@app.route('/uploads/<name>')
def download_file(name):
    return send_from_directory(app.config["UPLOAD_FOLDER"], name)

# [기존] 회원가입 (유지)
@app.route('/api/auth/signup', methods=['POST'])
def api_signup():
    d = request.json
    uid = d.get('id', '').strip().lower()
    pw = d.get('password', '').strip()
    nick = d.get('nickname', 'User').strip()
    if not uid or not pw: return jsonify({"error": "ID/PW required"}), 400
    try:
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM USERS WHERE user_id=:1", [uid])
        if cursor.fetchone(): return jsonify({"error": "ID exists"}), 409
        cursor.execute("INSERT INTO USERS (user_id, password, nickname, role) VALUES (:1, :2, :3, 'user')", [uid, generate_password_hash(pw), nick])
        conn.commit()
        return jsonify({"message": "Signup Success"})
    except Exception as e: return jsonify({"error": str(e)}), 500

# [기존] 로그인 (유지)
@app.route('/api/auth/login', methods=['POST'])
def api_login():
    d = request.json
    uid = d.get('id', '').strip().lower()
    pw = d.get('password', '').strip()
    if not uid or not pw: return jsonify({"error": "ID/PW required"}), 400
    try:
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("SELECT user_id, password, nickname, profile_img, role FROM USERS WHERE user_id=:1", [uid])
        user = cursor.fetchone()
        if user and check_password_hash(user[1], pw):
            return jsonify({"message": "Login Success", "user": {"id": user[0], "nickname": user[2], "profile_img": user[3], "role": user[4]}})
        return jsonify({"error": "Invalid credentials"}), 401
    except Exception as e: return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)