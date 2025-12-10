import os
import requests
import oracledb
from flask import Flask, request, jsonify, g, send_from_directory, make_response
from flask_cors import CORS
from werkzeug.utils import secure_filename
from datetime import datetime

# 모듈 import
from config import UPLOAD_FOLDER, SPOTIFY_API_BASE
from database import get_db_connection, close_db, init_db_pool
from services import update_box_office_data
from utils import allowed_file, verify_turnstile, get_spotify_headers, get_current_weather, get_today_holiday

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
CORS(app)
app.teardown_appcontext(close_db)

with app.app_context():
    init_db_pool()

# ... (기존 인증 API - signup, login, profile, password 유지) ...
# ... (상단 생략, 기존 코드와 동일) ...

# =========================================================
# [매핑 데이터] API 응답값을 RDF로 변환하기 위한 규칙
# =========================================================
HOLIDAY_MAPPING = {
    "신정": {"tag": "tag:Rest", "date_type": "2"},
    "설날": {"tag": "tag:Family", "date_type": "2"},
    "삼일절": {"tag": "tag:Memorial", "date_type": "2"},
    "어린이날": {"tag": "tag:Exciting", "date_type": "2"},
    "광복절": {"tag": "tag:Memorial", "date_type": "2"},
    "추석": {"tag": "tag:Family", "date_type": "2"},
    "개천절": {"tag": "tag:Memorial", "date_type": "2"},
    "한글날": {"tag": "tag:Korea", "date_type": "2"},
    "크리스마스": {"tag": "tag:Christmas", "date_type": "2"},
    "석가탄신일": {"tag": "tag:Rest", "date_type": "2"}
}

WEATHER_MAPPING = {
    "Rain": {"label": "비", "tag": "tag:Rain", "code": "1"},
    "Snow": {"label": "눈", "tag": "tag:Snow", "code": "3"},
    "Clear": {"label": "맑음", "tag": "tag:Clear", "code": "0"}
}

# =========================================================
# 3. 데이터 제공 API (TTL 생성)
# =========================================================

@app.route('/api/admin/update-movies', methods=['POST'])
def api_update_movies():
    try:
        msg = update_box_office_data()
        return jsonify({"message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/data/box-office.ttl', methods=['GET'])
def get_box_office_ttl():
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT m.movie_id, m.title, m.rank, m.poster_url, 
                   t.track_id, t.track_title, t.artist_name, t.image_url, t.preview_url
            FROM MOVIES m
            JOIN MOVIE_OSTS mo ON m.movie_id = mo.movie_id
            JOIN TRACKS t ON mo.track_id = t.track_id
            ORDER BY m.rank ASC
        """)
        rows = cursor.fetchall()
        
        ttl_parts = [
            "@prefix schema: <http://schema.org/> .",
            "@prefix komc: <https://knowledgemap.kr/komc/def/> .",
            "@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .",
            ""
        ]
        
        for row in rows:
            mid, mtitle, rank, mposter, tid, ttitle, artist, tcover, audio = row
            ttl_parts.append(f"""<https://knowledgemap.kr/resource/movie/{mid}> a schema:Movie ;
    schema:name "{mtitle}" ;
    schema:image "{mposter}" ;
    komc:rank {rank} .""")
            ttl_parts.append(f"""<https://knowledgemap.kr/resource/track/{tid}> a schema:MusicRecording ;
    schema:name "{ttitle}" ;
    schema:byArtist "{artist}" ;
    schema:image "{tcover}" ;
    schema:audio "{audio}" ;
    komc:featuredIn <https://knowledgemap.kr/resource/movie/{mid}> ;
    komc:relatedTag tag:MovieOST .""")
        
        return make_response("\n".join(ttl_parts), 200, {'Content-Type': 'text/turtle; charset=utf-8'})
    except Exception as e: return str(e), 500

@app.route('/api/recommend/context', methods=['GET'])
def get_context_recommendation():
    """
    [핵심] 실시간 상황별 추천 API (Dynamic RDF Generation)
    1. 외부 API로 날씨/휴일 정보 수집
    2. 조건 판단 (휴일 > 날씨 > 시간)
    3. DB에서 추천 곡 검색
    4. TTL 포맷으로 동적 생성하여 반환
    """
    try:
        # 1. 실시간 정보 수집
        weather_code = get_current_weather()  # Rain, Snow, Clear
        holiday_name = get_today_holiday()    # 휴일명 or None
        hour = datetime.now().hour

        # 2. 추천 로직 (SKOS)
        target_tag = "tag:Pop"
        context_uri = "https://knowledgemap.kr/komc/context/Day"
        pref_label = "일상"
        definition = "오늘 하루를 위한 음악"
        
        detected_triples = [] 

        # (1) 휴일 우선 적용
        if holiday_name:
            info = HOLIDAY_MAPPING.get(holiday_name, {"tag": "tag:Rest", "date_type": "2"})
            target_tag = info["tag"]
            context_uri = f"http://knowledgemap.kr/komc/holiday/{holiday_name}"
            pref_label = f"특별한 날 ({holiday_name})"
            definition = f"오늘은 {holiday_name}! 즐거운 하루 보내세요 🎉"
            
            detected_triples.append(f"<{context_uri}> a komc:HolidayContext ;")
            detected_triples.append(f"    schema:name \"{holiday_name}\" ;")
            detected_triples.append(f"    komc:datetype \"{info['date_type']}\" ;")
            detected_triples.append(f"    skos:link <https://knowledgemap.kr/komc/def/{target_tag.split(':')[1]}> .")

        # (2) 날씨 적용
        elif weather_code in ['Rain', 'Snow']:
            info = WEATHER_MAPPING[weather_code]
            target_tag = info["tag"]
            context_uri = f"https://knowledgemap.kr/komc/weather/{weather_code}"
            pref_label = f"{info['label']} 오는 날"
            definition = f"창밖의 {info['label']}와 어울리는 감성 ☔"
            
            detected_triples.append(f"<{context_uri}> a schema:WeatherForecast ;")
            detected_triples.append(f"    schema:weatherCondition \"{info['label']}\" ;")
            detected_triples.append(f"    komc:pty \"{info['code']}\" ;")
            detected_triples.append(f"    komc:relatedTag {target_tag} .")

        # (3) 시간대 적용
        else:
            time_slot = "Night" if (22 <= hour or hour < 6) else "Day"
            if 6 <= hour < 12: time_slot = "Morning"
            elif 18 <= hour < 22: time_slot = "Evening"
            
            context_uri = f"https://knowledgemap.kr/komc/time/{time_slot}"
            tag_map = {"Morning": "tag:Clear", "Day": "tag:Exciting", "Evening": "tag:Sentimental", "Night": "tag:Rest"}
            target_tag = tag_map.get(time_slot, "tag:Pop")
            
            pref_label = f"{time_slot}"
            definition = {
                "Morning": "상쾌한 아침을 여는 시작! ☀️",
                "Day": "활기찬 오후 에너지 충전 ⚡",
                "Evening": "하루를 마무리하는 감성 🌇",
                "Night": "깊은 밤, 편안한 휴식 🌙"
            }.get(time_slot, "음악과 함께하는 시간")
            
            detected_triples.append(f"<{context_uri}> a komc:TimeContext ;")
            detected_triples.append(f"    skos:prefLabel \"{time_slot}\" .")

        # 3. DB에서 추천 곡 랜덤 5개 추출
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM (
                SELECT t.track_id, t.track_title, t.artist_name, t.image_url, t.preview_url
                FROM TRACKS t
                JOIN TRACK_TAGS tt ON t.track_id = tt.track_id
                WHERE tt.tag_id = :1
                ORDER BY dbms_random.value
            ) WHERE ROWNUM <= 5
        """, [target_tag])
        rows = cursor.fetchall()

        # 4. TTL 조립
        ttl_parts = [
            "@prefix schema: <http://schema.org/> .",
            "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .",
            "@prefix komc: <https://knowledgemap.kr/komc/def/> .",
            "@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .",
            "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .",
            "",
            "# Generated dynamically based on Open API Data",
            ""
        ]
        
        ttl_parts.extend(detected_triples)
        
        ttl_parts.append(f"""
komc:CurrentContext a skos:Concept ;
    skos:prefLabel "{pref_label}"@ko ;
    skos:definition "{definition}"@ko ;
    komc:derivedFrom <{context_uri}> .""")

        track_uris = []
        for r in rows:
            tid, title, artist, cover, preview = r
            track_uri = f"<https://knowledgemap.kr/resource/track/{tid}>"
            track_uris.append(track_uri)
            ttl_parts.append(f"""
{track_uri} a schema:MusicRecording ;
    schema:name "{title}" ;
    schema:byArtist "{artist}" ;
    schema:image "{cover}" ;
    schema:audio "{preview}" .""")
        
        if track_uris:
            ttl_parts.append(f"komc:CurrentContext komc:recommends {', '.join(track_uris)} .")

        return make_response("\n".join(ttl_parts), 200, {'Content-Type': 'text/turtle; charset=utf-8'})

    except Exception as e:
        print(f"[Context Gen Error] {e}")
        return str(e), 500

# =========================================================
# 4. 검색 & 파일 제공 API
# =========================================================
@app.route('/api/search', methods=['GET'])
def proxy_search():
    try:
        q = request.args.get('q'); offset = request.args.get('offset', '0')
        if not q: return jsonify({"error": "No query"}), 400
        headers = get_spotify_headers()
        params = {"q": q, "type": "track,album,artist", "limit": "20", "offset": offset, "market": "KR"}
        res = requests.get(f"{SPOTIFY_API_BASE}/search", headers=headers, params=params)
        return jsonify(res.json()), res.status_code
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)