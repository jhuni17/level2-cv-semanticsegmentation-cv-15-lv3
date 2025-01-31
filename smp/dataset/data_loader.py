import os

def get_image_label_paths(IMAGE_ROOT, LABEL_ROOT=None):
    # 이미지 파일 경로 수집
    pngs = {
        os.path.relpath(os.path.join(root, fname), start=IMAGE_ROOT)
        for root, _dirs, files in os.walk(IMAGE_ROOT)
        for fname in files
        if os.path.splitext(fname)[1].lower() == ".png"
    }

    if LABEL_ROOT:
        # 라벨 파일 경로 수집
        jsons = {
            os.path.relpath(os.path.join(root, fname), start=LABEL_ROOT)
            for root, _dirs, files in os.walk(LABEL_ROOT)
            for fname in files
            if os.path.splitext(fname)[1].lower() == ".json"
        }

        # 접두어만 추출
        jsons_fn_prefix = {os.path.splitext(fname)[0] for fname in jsons}
        pngs_fn_prefix = {os.path.splitext(fname)[0] for fname in pngs}

        # 이미지와 라벨의 접두어가 정확히 일치하는지 확인
        assert len(jsons_fn_prefix - pngs_fn_prefix) == 0
        assert len(pngs_fn_prefix - jsons_fn_prefix) == 0

        # 경로 정렬
        pngs = sorted(pngs)
        jsons = sorted(jsons)
    else:
        # LABEL_ROOT가 없을 경우 라벨 경로를 빈 리스트로 반환
        jsons = []

    return pngs, jsons
