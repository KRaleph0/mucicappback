import requests
import datetime
import oracledb
import config
from database import get_db_connection

# ---------------------------------------------------------
# 1. TMDB 포스터 검색
# ---------------------------------------------------------
def get_tmdb_poster(movie_title):
    if not config.TMDB_API_KEY:
        print(f"⚠️ [TMDB] API Key가 없습니다. (Title: {movie_title})")
        return None
    
    try:
        url = "https://api.themoviedb.org/3/search/movie"
        params = { "api_key": config.TMDB_API_KEY, "query": movie_title, "language": "ko-KR", "page": 1 }
        res = requests.get(url, params=params, timeout=5)
        data = res.json()
        if data.get("results"):
            path = data["results"][0].get("poster_path")
            if path: return f"https://image.tmdb.org/t/p/w500{path}"
    except Exception as e:
        print(f"❌ [TMDB] 에러 발생 ({movie_title}): {e}")
    return None

# ---------------------------------------------------------
# 2. 박스오피스 업데이트
# ---------------------------------------------------------
def update_box_office_data():
    if not config.KOBIS_API_KEY: return "Key Error"
    
    yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
    target_dt = yesterday.strftime("%Y%m%d")
    
    try:
        res = requests.get(config.KOBIS_BOXOFFICE_URL, params={"key": config.KOBIS_API_KEY, "targetDt": target_dt})
        daily_list = res.json().get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])
        
        if not daily_list: return "No Data"

        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("DELETE FROM MOVIES") 
        
        count = 0
        for item in daily_list:
            rank = int(item['rank']); title = item['movieNm']; mid = item['movieCd']
            poster = get_tmdb_poster(title) or "img/playlist-placeholder.png"
            cur.execute("INSERT INTO MOVIES (movie_id, title, rank, poster_url) VALUES (:1, :2, :3, :4)", [mid, title, rank, poster])
            count += 1
            
        conn.commit(); conn.close()
        return f"Updated {count} movies."
    except Exception as e: return f"Error: {str(e)}"

# ---------------------------------------------------------
# 3. Spotify 트랙 정보 저장 (수정됨)
# ---------------------------------------------------------
def save_track_details(track_id, cur, headers, genre_seeds=[]):
    # 1. DB 확인
    cur.execute("SELECT track_title FROM TRACKS WHERE track_id=:1", [track_id])
    row = cur.fetchone()
    
    # [핵심 수정] 이미 존재하더라도, 이름이 'Unknown'이면 다시 가져오도록 통과시킴!
    # row[0]이 존재하고 'Unknown'이 아닐 때만 "이미 있다"고 판단함.
    if row and row[0] and row[0] != 'Unknown':
        return {"status": "exists", "name": row[0]}

    # 2. Spotify API 호출 (없거나 Unknown이면 실행)
    try:
        r = requests.get(f"{config.SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers)
        if r.status_code != 200: return None
        d = r.json()

        title = d['name']
        artist = d['artists'][0]['name']
        album_id = d['album']['id']
        preview = d.get('preview_url')
        img = d['album']['images'][0]['url'] if d['album']['images'] else None
        duration = d['duration_ms']

        f_res = requests.get(f"{config.SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers)
        feat = f_res.json() if f_res.status_code == 200 else {}
        bpm = feat.get('tempo', 0)
        key = str(feat.get('key', -1))

        # 3. DB 저장 (기존 'Unknown' 데이터가 있을 수 있으므로 삭제 후 삽입)
        cur.execute("DELETE FROM TRACKS WHERE track_id=:1", [track_id])
        cur.execute("""
            INSERT INTO TRACKS (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, music_key, duration, views)
            VALUES (:1, :2, :3, :4, :5, :6, :7, :8, :9, 0)
        """, [track_id, title, artist, album_id, preview, img, bpm, key, duration])
        
        return {"status": "saved", "name": title}

    except Exception as e:
        print(f"❌ Track Save Error: {e}")
        return None