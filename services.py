import requests
from datetime import datetime, timedelta
import config
import database
import utils

# ---------------------------------------------
# 1. 트랙 상세 정보 저장 (유저 매칭 시 사용)
# ---------------------------------------------
def save_track_details(track_id, cursor, headers, genres=[]):
    if not track_id: return None
    try:
        # Spotify API 호출
        t_res = requests.get(f"{config.SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers)
        if t_res.status_code != 200: return None
        t_data = t_res.json()
        
        # 오디오 특징 호출
        a_res = requests.get(f"{config.SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers)
        a_data = a_res.json() if a_res.status_code == 200 else {}

        # 데이터 추출
        title = t_data.get('name', 'Unknown')
        artist = t_data['artists'][0]['name'] if t_data.get('artists') else 'Unknown'
        prev = t_data.get('preview_url', '')
        aid = t_data.get('album', {}).get('id')
        img = t_data.get('album', {}).get('images', [{}])[0].get('url', '')
        bpm = a_data.get('tempo', 0)
        
        # 1. 앨범 저장
        if aid:
            cursor.execute("MERGE INTO ALBUMS USING dual ON (album_id=:1) WHEN NOT MATCHED THEN INSERT (album_id, album_cover_url) VALUES (:1, :2)", [aid, img])
        
        # 2. 트랙 저장
        cursor.execute("""
            MERGE INTO TRACKS t USING dual ON (t.track_id=:tid)
            WHEN MATCHED THEN UPDATE SET t.image_url=:img, t.preview_url=:prev
            WHEN NOT MATCHED THEN INSERT (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, views)
            VALUES (:tid, :title, :artist, :aid, :prev, :img, :bpm, 0)
        """, {'tid':track_id, 'title':title, 'artist':artist, 'aid':aid, 'prev':prev, 'img':img, 'bpm':bpm})

        # 3. 태그 생성 및 저장
        tags = set(["tag:Spotify"])
        # 영화 장르 매핑
        g_map = {"액션":"tag:Action", "SF":"tag:SF", "코미디":"tag:Exciting", "드라마":"tag:Sentimental", "멜로":"tag:Romance", "로맨스":"tag:Romance", "공포":"tag:Tension", "호러":"tag:Tension", "스릴러":"tag:Tension", "범죄":"tag:Tension", "애니메이션":"tag:Animation", "가족":"tag:Rest", "뮤지컬":"tag:Pop"}
        for g in genres:
            for k, val in g_map.items(): 
                if k in g: tags.add(val)
        
        # 오디오 특징 태그
        if a_data:
            e = a_data.get('energy', 0); v = a_data.get('valence', 0)
            if e>0.7: tags.add('tag:Exciting')
            if e<0.4: tags.add('tag:Rest')
            if v<0.3: tags.add('tag:Sentimental')
            if v>0.7: tags.add('tag:Pop')

        # 태그 DB 저장
        for t in tags:
            try:
                cursor.execute("""
                    MERGE INTO TRACK_TAGS t USING (SELECT :1 AS tid, :2 AS tag FROM dual) s 
                    ON (t.track_id = s.tid AND t.tag_id = s.tag) 
                    WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (s.tid, s.tag)
                """, [track_id, t])
            except: pass

        cursor.connection.commit()
        return t_data
    except Exception as e: 
        print(f"[Save Error] {e}")
        return None

# ---------------------------------------------
# 2. 박스오피스 업데이트 (영화 정보만 갱신)
# ---------------------------------------------
def update_box_office_data():
    print("[Batch] 박스오피스 업데이트 시작 (영화 정보만 갱신)...")
    conn = None
    try:
        conn = database.get_db_connection()
        cursor = conn.cursor()
        
        # 1. KOBIS에서 순위 가져오기
        target_dt = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        res = requests.get(config.KOBIS_BOXOFFICE_URL, params={"key": config.KOBIS_API_KEY, "targetDt": target_dt, "itemPerPage": "10"}, timeout=5).json()
        movie_list = res.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])
        
        if not movie_list: return "KOBIS 데이터 없음"

        for movie in movie_list:
            rank = int(movie["rank"])
            title = movie["movieNm"]
            kobis_code = movie["movieCd"]
            
            print(f"Processing [{rank}위]: {title}")

            # 2. TMDB에서 포스터 가져오기
            poster_url = None
            try:
                tmdb_res = requests.get("https://api.themoviedb.org/3/search/movie", 
                                      params={"api_key": config.TMDB_API_KEY, "query": title, "language": "ko-KR"}).json()
                if tmdb_res.get('results'):
                    top_match = tmdb_res['results'][0]
                    if top_match.get('poster_path'):
                        poster_url = f"https://image.tmdb.org/t/p/w500{top_match['poster_path']}"
            except Exception as e:
                print(f"   -> TMDB Error: {e}")

            # 3. DB 저장 (영화 정보만)
            try:
                cursor.execute("""
                    MERGE INTO MOVIES m
                    USING (SELECT :mid AS movie_id FROM dual) d
                    ON (m.movie_id = d.movie_id)
                    WHEN MATCHED THEN
                      UPDATE SET rank = :rank, poster_url = :poster, title = :title
                    WHEN NOT MATCHED THEN
                      INSERT (movie_id, title, rank, poster_url)
                      VALUES (:mid, :title, :rank, :poster)
                    """,
                    {"mid": kobis_code, "title": title, "rank": rank, "poster": poster_url}
                )
                conn.commit()
            except Exception as e:
                print(f"   -> DB Error: {e}")

        return "영화 정보 업데이트 완료 (OST 매칭 대기)"

    except Exception as e:
        print(f"[Batch Critical Error] {e}")
        return f"Error: {e}"
    finally:
        if conn: conn.close()