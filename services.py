import requests
import datetime
import oracledb
import config
from database import get_db_connection

# ---------------------------------------------------------
# 1. TMDB 포스터 검색
# ---------------------------------------------------------
def get_tmdb_poster(movie_title):
    # 키가 환경변수(docker-compose.yml)에 없으면 건너뜀
    if not config.TMDB_API_KEY:
        print("⚠️ TMDB_API_KEY가 설정되지 않았습니다.")
        return None
    
    try:
        url = "https://api.themoviedb.org/3/search/movie"
        params = {
            "api_key": config.TMDB_API_KEY,
            "query": movie_title,
            "language": "ko-KR",
            "page": 1
        }
        res = requests.get(url, params=params, timeout=3)
        data = res.json()
        
        if data.get("results"):
            path = data["results"][0].get("poster_path")
            if path:
                return f"https://image.tmdb.org/t/p/w500{path}"
    except Exception as e:
        print(f"⚠️ TMDB Error ({movie_title}): {e}")
    
    return None

# ---------------------------------------------------------
# 2. 박스오피스 업데이트 (KOBIS + TMDB)
# ---------------------------------------------------------
def update_box_office_data():
    if not config.KOBIS_API_KEY:
        return "❌ KOBIS_API_KEY가 설정되지 않았습니다."

    print("🚀 [Service] 박스오피스 업데이트 시작...")
    
    # 어제 날짜 구하기
    yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
    target_dt = yesterday.strftime("%Y%m%d")
    
    # config.py에 정의된 URL 상수 사용
    params = {
        "key": config.KOBIS_API_KEY,
        "targetDt": target_dt
    }
    
    try:
        res = requests.get(config.KOBIS_BOXOFFICE_URL, params=params)
        data = res.json()
        daily_list = data.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])
        
        if not daily_list:
            return "❌ KOBIS 데이터 응답 없음"

        conn = get_db_connection()
        cur = conn.cursor()
        
        # 기존 순위 데이터 삭제
        cur.execute("DELETE FROM MOVIES") 
        
        count = 0
        for item in daily_list:
            rank = int(item['rank'])
            title = item['movieNm']
            mid = item['movieCd']
            
            # TMDB 이미지 검색 (없으면 기본 이미지)
            poster = get_tmdb_poster(title)
            if not poster:
                poster = "img/playlist-placeholder.png"

            cur.execute("""
                INSERT INTO MOVIES (movie_id, title, rank, poster_url)
                VALUES (:1, :2, :3, :4)
            """, [mid, title, rank, poster])
            count += 1
            
        conn.commit()
        conn.close()
        return f"✅ {count}개 영화 업데이트 완료 (TMDB 이미지 적용)"

    except Exception as e:
        print(f"❌ Update Error: {e}")
        return f"Error: {str(e)}"

# ---------------------------------------------------------
# 3. Spotify 트랙 정보 저장
# ---------------------------------------------------------
def save_track_details(track_id, cur, headers, genre_seeds=[]):
    # 이미 존재하는지 확인
    cur.execute("SELECT track_title FROM TRACKS WHERE track_id=:1", [track_id])
    if cur.fetchone():
        return {"status": "exists", "name": "Unknown"}

    try:
        # config.py에 정의된 API Base URL 사용
        r = requests.get(f"{config.SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers)
        if r.status_code != 200: return None
        d = r.json()

        title = d['name']
        artist = d['artists'][0]['name']
        album_id = d['album']['id']
        preview = d.get('preview_url')
        img = d['album']['images'][0]['url'] if d['album']['images'] else None
        duration = d['duration_ms']

        # Audio Features
        f_res = requests.get(f"{config.SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers)
        feat = f_res.json() if f_res.status_code == 200 else {}
        bpm = feat.get('tempo', 0)
        key = str(feat.get('key', -1))

        # DB 저장
        cur.execute("""
            INSERT INTO TRACKS (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, music_key, duration, views)
            VALUES (:1, :2, :3, :4, :5, :6, :7, :8, :9, 0)
        """, [track_id, title, artist, album_id, preview, img, bpm, key, duration])
        
        return {"status": "saved", "name": title}

    except Exception as e:
        print(f"❌ Track Save Error: {e}")
        return None