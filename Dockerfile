# 1. 베이스 이미지 (파이썬 3.10 슬림 버전)
FROM python:3.10-slim

# 2. 작업 폴더 설정
WORKDIR /app

# 3. 오라클 클라이언트 설치 (Zip 다운로드 대신 apt 저장소 방식 사용)
RUN apt-get update && apt-get install -y wget gpg ca-certificates libaio1 \
 && wget https://apt.oracle.com/CONTENT/GPG/oracle-hrms-pub-key.pub \
 && gpg --dearmor oracle-hrms-pub-key.pub --yes -o /usr/share/keyrings/oracle-hrms-pub-key.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/oracle-hrms-pub-key.gpg] https://apt.oracle.com/oracle-instantclient bullseye main" > /etc/apt/sources.list.d/oracle-instantclient.list \
 && apt-get update \
 && apt-get install -y oracle-instantclient-basic \
 && rm oracle-hrms-pub-key.pub \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# 4. 파이썬 라이브러리 설치 (requirements.txt 파일이 필요합니다)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. 나머지 코드 복사 (app.py 등)
COPY . .

# 6. 서버 실행 (예: Flask)
# (app.py 파일을 5000번 포트로 실행)
CMD ["flask", "--app", "app", "run", "--host=0.0.0.0", "--port=5000"]