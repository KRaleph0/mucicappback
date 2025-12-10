# services.py
import requests
from datetime import datetime, timedelta
import config
import database
import utils

def find_best_track(titles, headers):
    candidates = []
    seen = set()
    for t in titles:
        if t and t not in seen: candidates.append(t); seen.add(t)
    for title in candidates:
        try:
            res = requests.get(f"{config.SPOTIFY_API_BASE}/search", headers=headers, params={"q": f"{title} ost", "type": "track", "limit": 5, "market": "KR"}).json()
            for track in res.get('tracks', {}).get('items', []):
                sim = max(utils.get_similarity(title, track['name']), utils.get_similarity(title, track['album']['name']))
                if sim >= 0.5: return track
        except: pass
    return None

def save_track_details(track_id, cursor, headers, genres=[]):
    if not track_id: return None
    try:
        t_res = requests.get(f"{config.SPOTIFY_API_BASE}/tracks/{track_id}", headers=headers)
        if t_res.status_code != 200: return None
        t_data = t_res.json()
        a_res = requests.get(f"{config.SPOTIFY_API_BASE}/audio-features/{track_id}", headers=headers)
        a_data = a_res.json() if a_res.status_code == 200 else {}

        title = t_data.get('name', 'Unknown')
        artist = t_data['artists'][0]['name'] if t_data.get('artists') else 'Unknown'
        prev = t_data.get('preview_url', '')
        aid = t_data.get('album', {}).get('id')
        img = t_data.get('album', {}).get('images', [{}])[0].get('url', '')
        bpm = a_data.get('tempo', 0); k_int = a_data.get('key', -1); dur = utils.ms_to_iso_duration(t_data.get('duration_ms', 0))
        mkey = config.PITCH_CLASS[k_int] if 0 <= k_int < 12 else 'Unknown'

        if aid:
            cursor.execute("MERGE INTO ALBUMS USING dual ON (album_id=:1) WHEN NOT MATCHED THEN INSERT (album_id, album_cover_url) VALUES (:1, :2)", [aid, img])
        
        cursor.execute("""
            MERGE INTO TRACKS t USING dual ON (t.track_id=:tid)
            WHEN MATCHED THEN UPDATE SET t.bpm=:bpm, t.music_key=:mkey, t.duration=:dur, t.image_url=:img
            WHEN NOT MATCHED THEN INSERT (track_id, track_title, artist_name, album_id, preview_url, image_url, bpm, music_key, duration, views)
            VALUES (:tid, :title, :artist, :aid, :prev, :img, :bpm, :mkey, :dur, 0)
        """, {'tid':track_id, 'title':title, 'artist':artist, 'aid':aid, 'prev':prev, 'img':img, 'bpm':bpm, 'mkey':mkey, 'dur':dur})

        tags = set(["tag:Spotify"])
        if genres: tags.add("tag:MovieOST")
        e = a_data.get('energy', 0); v = a_data.get('valence', 0)
        if e>0.7: tags.add('tag:Exciting')
        if e<0.4: tags.add('tag:Rest')
        if v<0.3: tags.add('tag:Sentimental')
        if v>0.7: tags.add('tag:Pop')
        
        g_map = {"액션":"tag:Action", "SF":"tag:SF", "코미디":"tag:Exciting", "드라마":"tag:Sentimental", "멜로":"tag:Romance", "로맨스":"tag:Romance", "공포":"tag:Tension", "호러":"tag:Tension", "스릴러":"tag:Tension", "범죄":"tag:Tension", "애니메이션":"tag:Animation", "가족":"tag:Rest", "뮤지컬":"tag:Pop"}
        for g in genres:
            for k,val in g_map.items(): 
                if k in g: tags.add(val)
        
        for t in tags:
            try: cursor.execute("MERGE INTO TRACK_TAGS USING dual ON (track_id=:1 AND tag_id=:2) WHEN NOT MATCHED THEN INSERT (track_id, tag_id) VALUES (:1, :2)", [track_id, t])
            except: pass
        
        cursor.connection.commit()
        return t_data
    except Exception as e: return None

def update_box_office_data():
    print("[Batch] 박스오피스 업데이트 시작...")
    try:
        conn = database.get_db_connection()
        cursor = conn.cursor()
        headers = utils.get_spotify_headers()
        target_dt = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
        res = requests.get(config.KOBIS_BOXOFFICE_URL, params={"key": config.KOBIS_API_KEY, "targetDt": target_dt, "itemPerPage": "10"}).json()
        movie_list = res.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])
        if not movie_list: return "데이터 없음"
        for movie in movie_list:
            rank = int(movie['rank']); title = movie['movieNm']
            print(f"Processing: {title}")
            genres, title_en, title_og = utils.get_kobis_metadata(title)
            poster_url = None
            try:
                tmdb_res = requests.get("https://api.themoviedb.org/3/search/movie", params={"api_key": config.TMDB_API_KEY, "query": title, "language": "ko-KR"}).json()
                if tmdb_res.get('results'): poster_url = f"https://image.tmdb.org/t/p/w500{tmdb_res['results'][0]['poster_path']}"
            except: pass
            try:
                cursor.execute("MERGE INTO MOVIES m USING (SELECT :mid AS mid FROM dual) d ON (m.movie_id=d.mid) WHEN MATCHED THEN UPDATE SET rank=:rank, poster_url=:poster WHEN NOT MATCHED THEN INSERT (movie_id, title, rank, poster_url) VALUES (:mid, :title, :rank, :poster)", {'mid':title, 'title':title, 'rank':rank, 'poster':poster_url})
                conn.commit()
            except: pass
            matched_track = find_best_track([title_og, title_en, title], headers)
            if matched_track:
                tid = matched_track['id']
                save_track_details(tid, cursor, headers, genres)
                try:
                    cursor.execute("DELETE FROM MOVIE_OSTS WHERE movie_id=:mid", {'mid':title})
                    cursor.execute("INSERT INTO MOVIE_OSTS (movie_id, track_id) VALUES (:mid, :tid)", {'mid':title, 'tid':tid})
                    conn.commit()
                except: pass
        return "업데이트 완료"
    except Exception as e: return f"Error: {e}"