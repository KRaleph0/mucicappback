from rdflib import Graph, Namespace, RDF, SKOS, Literal

class SkosManager:
    def __init__(self, file_path):
        self.g = Graph()
        try:
            self.g.parse(file_path, format="turtle")
            print(f"✅ [SKOS] '{file_path}' 로드 성공! (트리플 수: {len(self.g)})")
        except Exception as e:
            print(f"❌ [SKOS] 로드 실패: {e}")

        self.KOMC = Namespace("https://knowledgemap.kr/komc/def/")
    
    def _normalize(self, text):
        """대소문자 무시 및 공백 제거"""
        if not text: return ""
        return text.replace("tag:", "").strip().lower()

    def _find_concept_uri(self, keyword):
        """키워드(ID 또는 라벨)로 개념 URI 찾기 (Genre 우선 순위 적용)"""
        target = self._normalize(keyword)
        candidates = []
        
        # 1. URI의 끝부분(ID)으로 검색
        for s in self.g.subjects(RDF.type, SKOS.Concept):
            # URI 파싱 안전장치 추가
            try:
                if str(s).split("_")[-1].lower() == target:
                    candidates.append(s)
            except: continue
        
        # 2. 라벨(prefLabel)로 검색 (후보가 없을 때만)
        if not candidates:
            for s, p, o in self.g.triples((None, SKOS.prefLabel, None)):
                if str(o).lower() == target:
                    candidates.append(s)
        
        if not candidates:
            print(f"⚠️ [SKOS] '{keyword}'에 대한 개념을 찾을 수 없음")
            return None

        # [디버깅 로그] 어떤 후보들이 발견되었는지 출력
        # print(f"🔍 [SKOS Debug] '{keyword}' 후보군: {[str(c).split('/')[-1] for c in candidates]}")

        # [핵심 수정] Genre_나 Weather_가 포함된 개념(계층 구조가 있는 개념)을 우선 반환
        for uri in candidates:
            uri_str = str(uri)
            if "Genre_" in uri_str or "Weather_" in uri_str:
                print(f"✅ [SKOS] '{keyword}' -> 계층 개념 선택됨: {uri_str.split('/')[-1]}")
                return uri

        # 계층 개념이 없으면 첫 번째 것 반환 (예: 말단 태그)
        selected = candidates[0]
        print(f"ℹ️ [SKOS] '{keyword}' -> 일반 개념 선택됨: {str(selected).split('/')[-1]}")
        return selected

    def _get_all_labels(self, uri):
        """특정 개념의 ID와 모든 라벨(한/영)을 반환"""
        labels = set()
        try:
            # ID 추가 (예: JPop)
            labels.add(str(uri).split("_")[-1])
            # 라벨 추가 (예: J-Pop, Jpop)
            for lbl in self.g.objects(uri, SKOS.prefLabel):
                labels.add(str(lbl))
        except: pass
        return labels

    def get_broader_tags(self, tag):
        """상위 개념 찾기 (저장용)"""
        uri = self._find_concept_uri(tag)
        if not uri: return set()
        
        broader_tags = set()
        for parent in self.g.objects(uri, SKOS.broader):
            broader_tags.add(str(parent).split("_")[-1])
        
        for rel in self.g.objects(uri, SKOS.related):
            broader_tags.add(str(rel).split("_")[-1])
            
        return broader_tags

    def get_narrower_tags(self, tag):
        """하위 개념 및 동의어 찾기 (검색용)"""
        root = self._find_concept_uri(tag)
        expanded_tags = {self._normalize(tag)}
        
        if not root: return list(expanded_tags)

        # 1. 루트 개념 라벨 추가
        expanded_tags.update(self._get_all_labels(root))

        # 2. 하위 개념 재귀 탐색
        def traverse(node):
            # narrower (하위 장르)
            for child in self.g.objects(node, SKOS.narrower):
                expanded_tags.update(self._get_all_labels(child))
                # 하위 장르의 연관 태그 (Genre_JPop -> tag_Jpop)
                for rel in self.g.objects(child, SKOS.related):
                    expanded_tags.update(self._get_all_labels(rel))
                traverse(child)
            
            # [중요] 루트/현재 노드의 related 태그도 검색 범위에 포함 (Genre_Pop -> tag_Pop)
            for rel in self.g.objects(node, SKOS.related):
                expanded_tags.update(self._get_all_labels(rel))

        traverse(root)
        
        # 3. 결과 반환
        result_list = list(expanded_tags)
        print(f"🚀 [SKOS Expansion] '{tag}' -> {len(result_list)}개 확장: {result_list}")
        return result_list
    
    def get_weather_tags(self, weather_keyword):
        uri = self.KOMC[f"Weather_{weather_keyword}"]
        if (uri, RDF.type, SKOS.Concept) not in self.g: uri = self.KOMC["Weather_Default"]
        tags = []
        for rel in self.g.objects(uri, SKOS.related):
            for lbl in self.g.objects(rel, SKOS.prefLabel):
                if lbl.language == 'ko': tags.append(str(lbl))
        return tags