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
from utils import (
    allowed_file, verify_turnstile, get_spotify_headers, 
    get_current_weather, get_today_holiday,
    get_similarity, clean_text 
)

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
CORS(app)
app.teardown_appcontext(close_db)

with app.app_context():
    init_db_pool()

# ... (기존 1. 인증 API, 2. 사용자 API, 3. 박스오피스 API 등은 그대로 유지) ...
# (상단 생략: signup, login, handle_profile, update_password, api_update_movies, get_box_office_ttl 등은 기존 코드 사용)

# =========================================================
# [수정] 상황별 추천 API (태그 추천 강화)
# =========================================================
# ... (HOLIDAY_MAPPING, WEATHER_MAPPING 딕셔너리 등 상단 정의 필요) ...
HOLIDAY_MAPPING = {
    "신정": {"tag": "tag:Rest", "date_type": "2"},
    "어린이날": {"tag": "tag:Exciting", "date_type": "2"},
    "크리스마스": {"tag": "tag:Christmas", "date_type": "2"},
    # ... (기존 매핑 유지) ...
}
WEATHER_MAPPING = {
    "Rain": {"label": "비", "tag": "tag:Rain", "code": "1"},
    "Snow": {"label": "눈", "tag": "tag:Snow", "code": "3"},
    "Clear": {"label": "맑음", "tag": "tag:Clear", "code": "0"}
}

@app.route('/api/recommend/context', methods=['GET'])
def get_context_recommendation():
    try:
        # 1. 정보 수집
        weather_code = get_current_weather()
        holiday_name = get_today_holiday()
        hour = datetime.now().hour

        # 2. 로직 판단
        target_tag = "tag:Pop"
        context_uri = "https://knowledgemap.kr/komc/context/Day"
        pref_label = "일상"
        definition = "오늘 하루를 위한 태그"
        
        detected_triples = [] 

        if holiday_name:
            info = HOLIDAY_MAPPING.get(holiday_name, {"tag": "tag:Rest"})
            target_tag = info["tag"]
            context_uri = f"http://knowledgemap.kr/komc/holiday/{holiday_name}"
            pref_label = f"{holiday_name}"
            definition = f"오늘은 {holiday_name}! 이런 음악 어때요?"
            
            detected_triples.append(f"<{context_uri}> a komc:HolidayContext ;")
            detected_triples.append(f"    schema:name \"{holiday_name}\" ;")
            detected_triples.append(f"    komc:relatedTag {target_tag} .")

        elif weather_code in ['Rain', 'Snow']:
            info = WEATHER_MAPPING[weather_code]
            target_tag = info["tag"]
            context_uri = f"https://knowledgemap.kr/komc/weather/{weather_code}"
            pref_label = f"{info['label']} 오는 날"
            definition = f"창밖의 날씨와 어울리는 무드"
            
            detected_triples.append(f"<{context_uri}> a schema:WeatherForecast ;")
            detected_triples.append(f"    schema:weatherCondition \"{info['label']}\" ;")
            detected_triples.append(f"    komc:relatedTag {target_tag} .")

        else:
            time_slot = "Night" if (22 <= hour or hour < 6) else "Day"
            if 6 <= hour < 12: time_slot = "Morning"
            elif 18 <= hour < 22: time_slot = "Evening"
            
            context_uri = f"https://knowledgemap.kr/komc/time/{time_slot}"
            tag_map = {"Morning": "tag:Clear", "Day": "tag:Exciting", "Evening": "tag:Sentimental", "Night": "tag:Rest"}
            target_tag = tag_map.get(time_slot, "tag:Pop")
            
            pref_label = f"{time_slot}"
            definition = "지금 시간대에 딱 맞는 분위기"
            
            detected_triples.append(f"<{context_uri}> a komc:TimeContext ;")
            detected_triples.append(f"    skos:prefLabel \"{time_slot}\" ;")
            detected_triples.append(f"    komc:relatedTag {target_tag} .")

        # 3. TTL 생성 (태그 정보 포함)
        ttl_parts = [
            "@prefix schema: <http://schema.org/> .",
            "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .",
            "@prefix komc: <https://knowledgemap.kr/komc/def/> .",
            "@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .",
            "",
            "# Context-based Recommendation",
            ""
        ]
        
        ttl_parts.extend(detected_triples)
        
        # [핵심] komc:recommendedTag 속성 추가 (프론트가 쉽게 읽도록)
        ttl_parts.append(f"""
komc:CurrentContext a skos:Concept ;
    skos:prefLabel "{pref_label}"@ko ;
    skos:definition "{definition}"@ko ;
    komc:derivedFrom <{context_uri}> ;
    komc:recommendedTag {target_tag} .""")

        return make_response("\n".join(ttl_parts), 200, {'Content-Type': 'text/turtle; charset=utf-8'})

    except Exception as e:
        print(f"[Context Error] {e}")
        return str(e), 500

# =========================================================
# [신규] 6. 개별 곡 정보 TTL API (조회수 포함)
# =========================================================
@app.route('/api/track/<track_id>.ttl', methods=['GET'])
def get_track_detail_ttl(track_id):
    """특정 곡 정보를 조회수(views) 포함하여 TTL로 반환"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 1. 조회수 증가 (상세 조회 시 +1)
        cursor.execute("UPDATE TRACKS SET views = views + 1 WHERE track_id = :1", [track_id])
        conn.commit()

        # 2. 정보 조회
        cursor.execute("""
            SELECT t.track_title, t.artist_name, t.album_id, t.preview_url, t.image_url, 
                   t.bpm, t.music_key, t.duration, t.views
            FROM TRACKS t WHERE t.track_id = :1
        """, [track_id])
        row = cursor.fetchone()
        
        if not row: return "Track not found", 404
        
        title, artist, aid, prev, cover, bpm, key, dur, views = row
        
        # 태그 조회
        cursor.execute("SELECT tag_id FROM TRACK_TAGS WHERE track_id = :1", [track_id])
        tags = [r[0] for r in cursor.fetchall()]
        tag_str = ", ".join(tags) if tags else "tag:Music"

        # 3. TTL 생성
        ttl = f"""@prefix schema: <http://schema.org/> .
@prefix mo: <http://purl.org/ontology/mo/> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix komc: <https://knowledgemap.kr/komc/def/> .
@prefix tag: <https://knowledgemap.kr/komc/def/tag/> .

<https://knowledgemap.kr/resource/track/{track_id}>
    a schema:MusicRecording ;
    schema:name "{title}" ;
    schema:byArtist "{artist}" ;
    schema:image "{cover}" ;
    schema:audio "{prev or ''}" ;
    mo:bpm "{bpm}"^^xsd:integer ;
    mo:key "{key}" ;
    
    # 조회수 (View Count)
    komc:playCount "{views}"^^xsd:integer ;
    
    # 태그 정보
    komc:relatedTag {tag_str} .
"""
        return make_response(ttl, 200, {'Content-Type': 'text/turtle; charset=utf-8'})
    except Exception as e: return str(e), 500

# ... (기존 proxy_search, uploaded_file 등 유지) ...

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)