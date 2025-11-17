# 1. 베이스 이미지 (파이썬 3.10 슬림 버전)
FROM python:3.10-slim

# 2. 작업 폴더 설정
WORKDIR /app
RUN apt-get update \
    && apt-get install -y wget unzip ca-certificates libaio1 || apt-get install -y libaio \
    && mkdir -p /opt/oracle \
    && wget --progress=dot:giga https://download.oracle.com/otn_software/linux/instantclient/instantclient-basiclite-linuxx64.zip -O /opt/oracle/instantclient.zip \
    && unzip /opt/oracle/instantclient.zip -d /opt/oracle \
    && rm /opt/oracle/instantclient.zip \


# 4. 파이썬 라이브러리 설치 (requirements.txt 파일이 필요합니다)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. 나머지 코드 복사 (app.py 등)
COPY . .

# 6. 서버 실행 (예: Flask)
# (app.py 파일을 5000번 포트로 실행)
CMD ["flask", "--app", "app", "run", "--host=0.0.0.0", "--port=5000"]