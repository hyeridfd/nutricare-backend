-- ════════════════════════════════════════════════════════════
-- 마이그레이션: facilities 테이블에 로그인 정보 추가
-- ════════════════════════════════════════════════════════════
-- 기존 supabase_schema.sql 실행 후, 이 파일을 SQL Editor에서 추가로 실행하세요.
-- 비밀번호는 평문으로 저장하지 않고 해시(bcrypt)로 저장합니다.

alter table facilities
  add column login_id text unique,
  add column password_hash text;

-- 테스트 시설에 로그인 정보 부여 예시
-- (아래는 예시일 뿐, 실제 비밀번호 해시는 백엔드 /api/auth/setup-password 같은
--  엔드포인트로 생성하는 것을 권장. 직접 SQL로 넣으려면 bcrypt 해시값이 필요함)
--
-- update facilities
--   set login_id = 'demo-care-center', password_hash = '<bcrypt 해시>'
--   where id = '31216bcc-098f-428f-a278-21b3f0a878be';
