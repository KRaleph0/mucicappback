import requests
import datetime
import oracledb
import config
from database import get_db_connection

# ---------------------------------------------------------
# 1. TMDB 포스터 검색
# ---------------------------------------------------------
def get_tmdb_poster(movie_title):
    # 키 확인
    if not config.TMDB_API_KEY:
        print(f"⚠️ [TMDB] API Key가 없습니다. (Title: {movie_title})")
        return None
    
    try:
        url = "https://api.themoviedb.org/3/search/movie"
        params = {
            "api_key": config.TMDB_API_KEY,
            "query": movie_title,
            "language": "ko-KR",
            "page": 1
        }
        res = requests.get(url, params=params, timeout=5)
        
        if res.status_code != 200:
            print(f"⚠️ [TMDB] API 호출 실패: {res.status_code} - {res.text}")
            return None

        data = res.json()
        
        if data.get("results"):
            path = data["results"][0].get("poster_path")
            if path:
                print(f"   📸 [TMDB] 포스터 찾음: {movie_title}")
                return f"https://image.tmdb.org/t/p/w500{path}"
        
        print(f"   💨 [TMDB] 검색 결과 없음: {movie_title}")
    
    except Exception as e:
        print(f"❌ [TMDB] 에러 발생 ({movie_title}): {e}")
    
    return None

# ---------------------------------------------------------
# 2. 박스오피스 업데이트 (KOBIS + TMDB)
# ---------------------------------------------------------
def update_box_office_data():
    print("\n🚀 [Update] 박스오피스 업데이트 프로세스 시작...")

    # 1. API 키 확인
    if not config.KOBIS_API_KEY:
        print("❌ [Config] KOBIS_API_KEY가 없습니다.")
        return "Key Error"
    
    # 2. 날짜 설정 (어제 기준)
    yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
    target_dt = yesterday.strftime("%Y%m%d")
    print(f"📅 [KOBIS] 타겟 날짜: {target_dt}")
    
    # 3. KOBIS 호출
    params = {
        "key": config.KOBIS_API_KEY,
        "targetDt": target_dt
    }
    
    try:
        print(f"📡 [KOBIS] 데이터 요청 중... ({config.KOBIS_BOXOFFICE_URL})")
        res = requests.get(config.KOBIS_BOXOFFICE_URL, params=params)
        
        if res.status_code != 200:
            print(f"❌ [KOBIS] 요청 실패: {res.status_code}")
            return f"KOBIS API Error: {res.status_code}"

        data = res.json()
        daily_list = data.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])
        
        if not daily_list:
            print("❌ [KOBIS] 받아온 데이터 리스트가 비어있습니다. (혹시 오늘 날짜로 요청했나요?)")
            return "No Data"

        print(f"✅ [KOBIS] {len(daily_list)}개의 영화 데이터를 받았습니다.")

        # 4. DB 연결 및 저장
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("DELETE FROM MOVIES") 
        print("🗑️ [DB] 기존 영화 데이터 삭제 완료.")
        
        count = 0
        for item in daily_list:
            rank = int(item['rank'])
            title = item['movieNm']
            mid = item['movieCd']
            
            # TMDB 이미지 검색
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
        print(f"✨ [Success] 총 {count}개 영화 저장 완료!\n")
        return f"Updated {count} movies."

    except Exception as e:
        print(f"❌ [Critical Error]: {e}")
        return f"Error: {str(e)}"

# ---------------------------------------------------------
# 3. Spotify 트랙 정보 저장
# ---------------------------------------------------------
def save_track_details(track_id, cur, headers, genre_seeds=[]):
    cur.execute("SELECT track_title FROM TRACKS WHERE track_id=:1", [track_id])
    if cur.fetchone():
        return {"status": "exists", "name": "Unknown"}

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

        cur.execute("""
            INSERT INTO TRACKS (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, music_key, duration, views)
            VALUES (:1, :2, :3, :4, :5, :6, :7, :8, :9, 0)
        """, [track_id, title, artist, album_id, preview, img, bpm, key, duration])
        
        return {"status": "saved", "name": title}

    except Exception as e:
        print(f"❌ Track Save Error: {e}")
        return None