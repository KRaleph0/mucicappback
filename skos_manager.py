# skos_manager.py - 초간단 하드코딩 버전

class SkosManager:
    def __init__(self, file_path=None):
        # 여기에 [하위태그] : [상위태그 목록] 을 정의하세요.
        # "이 태그(Key)가 달리면 -> 저 태그들(Value)도 같이 달아라" 라는 뜻입니다.
        self.broader_map = {
            # 장르 계층
            "tag:K-Pop": ["tag:Pop"],
            "tag:J-Pop": ["tag:Pop"],
            "tag:HipHop": ["tag:Exciting"],
            "tag:Rock": ["tag:Exciting"],
            "tag:Ballad": ["tag:Sentimental"],
            "tag:R&B": ["tag:Sentimental", "tag:Pop"],
            "tag:Dance": ["tag:Exciting", "tag:Pop"],
            
            # 분위기 계층
            "tag:Rain": ["tag:Sentimental"],
            "tag:Snow": ["tag:Romance", "tag:Sentimental"],
            "tag:Party": ["tag:Exciting"],
            "tag:Morning": ["tag:Clear"],
            "tag:Night": ["tag:Rest", "tag:Sentimental"],
            "tag:Drive": ["tag:Exciting", "tag:Pop"],
        }
        print(f"✅ [SKOS] 임시 계층 구조 로드됨 ({len(self.broader_map)}개 규칙)")

    def get_broader_tags(self, tag):
        """
        입력된 태그(tag)와 그 상위 개념들을 모두 찾아서 리스트로 반환합니다.
        예: 'tag:K-Pop' 입력 -> ['tag:K-Pop', 'tag:Pop'] 반환
        """
        # 1. 기본적으로 자기 자신은 포함
        expanded_tags = {tag}
        
        # 2. 상위 개념이 있다면 추가
        if tag in self.broader_map:
            parents = self.broader_map[tag]
            expanded_tags.update(parents)
            # (옵션) 2단계 상위까지 찾으려면 여기서 parents를 순회하며 재귀 호출 가능
            
        return list(expanded_tags)

# 테스트용
if __name__ == "__main__":
    skos = SkosManager()
    print(skos.get_broader_tags("tag:K-Pop")) 
    # 출력: ['tag:K-Pop', 'tag:Pop']
