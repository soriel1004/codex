"""Windows shared-folder launcher and one-time server setup (standard library only)."""
import json
import os
import socket
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse
import app
import storage

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / 'server-config.json'


def read_config():
    if not CONFIG.exists():
        return None
    config = json.loads(CONFIG.read_text(encoding='utf-8-sig'))
    if not isinstance(config,dict) or any(not isinstance(config.get(key),str) or not config[key] for key in ('server_url','server_pc','data_dir','backup_dir')) or type(config.get('port')) is not int or not 1 <= config['port'] <= 65535:
        raise ValueError('서버 설정 파일 형식을 확인하세요. 기존 파일을 보관한 뒤 서버 설정을 다시 진행할 수 있습니다.')
    parsed = urlparse(config.get('server_url', ''))
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or '\n' in config['server_url'] or '\r' in config['server_url']:
        raise ValueError('server-config.json의 서버 주소를 확인하세요.')
    return config


def server_data(config):
    if config['server_pc'].casefold() != socket.gethostname().casefold():
        raise ValueError('서버 실행은 설정한 PC(' + config['server_pc'] + ')에서만 가능합니다. 이 PC에서는 start-client-windows.bat를 실행하세요.')
    data = Path(config['data_dir']).resolve()
    storage.require_local(data)
    if not (data/'as.db').is_file():
        raise ValueError('설정된 서버 DB를 찾지 못했습니다. 빈 DB를 새로 만들지 않습니다. 데이터 경로를 확인하거나 백업을 복원하세요.')
    with closing(sqlite3.connect(storage.readonly_uri(data/'as.db'),uri=True)) as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='slips'").fetchone():
            raise ValueError('설정된 서버 파일은 AS 데이터베이스가 아닙니다. 백업과 데이터 경로를 확인하세요.')
    return data


def import_legacy(source, target):
    source, target = Path(source).resolve(), Path(target).resolve()
    storage.require_local(target.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError('서버 데이터가 이미 있으므로 기존 파일을 덮어쓰지 않습니다.')
    temporary = target.with_suffix('.importing')
    try:
        with closing(sqlite3.connect(storage.readonly_uri(source), uri=True, timeout=5)) as src:
            if not src.execute("SELECT 1 FROM sqlite_master WHERE name='slips'").fetchone():
                raise ValueError('기존 AS 전표 데이터베이스가 아닙니다.')
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst, pages=256)
                dst.commit()
                if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('기존 데이터 무결성 검사에 실패했습니다.')
        if target.exists():
            raise ValueError('서버 데이터가 이미 있습니다. 가져오기를 중단합니다.')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def setup():
    print('이 PC 한 대만 서버로 지정합니다. 다른 PC는 브라우저로 접속합니다.')
    print('먼저 모든 PC에서 기존 프로그램의 커맨드 창을 Ctrl+C로 종료하세요.')
    if input('모든 기존 프로그램을 종료했습니까? (Y 입력): ').strip().lower() != 'y':
        return
    previous = read_config()
    if previous and previous['server_pc'].casefold() != socket.gethostname().casefold():
        raise ValueError('이미 다른 PC가 서버로 지정되어 있습니다. 서버 이전은 기존 서버를 종료하고 데이터를 옮긴 후 server-config.json을 별도로 보관하고 다시 설정하세요.')
    default = Path(previous['data_dir']) if previous else Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'ASManager' / 'data'
    data = Path(input(f'서버 로컬 데이터 폴더 [기본 {default}]: ').strip().strip('"') or default).resolve()
    storage.require_local(data)
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except OSError:
        ip = socket.gethostname()
    default_url = previous['server_url'] if previous else f'http://{ip}:8000'
    url = input(f'다른 PC에서 접속할 서버 주소 [{default_url}]: ').strip() or default_url
    parsed = urlparse(url)
    if parsed.scheme != 'http' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
        raise ValueError('http://서버PC주소:포트 형식으로 입력하세요.')
    port = parsed.port or 80
    default_backup = Path(previous['backup_dir']) if previous else ROOT / 'backups'
    backup = Path(input(f'자동 백업 폴더 [기본 {default_backup}]: ').strip().strip('"') or default_backup).resolve()
    with storage.ServerLock(data):
        if not (data / 'as.db').exists():
            default_source = Path(previous['data_dir']) / 'as.db' if previous else ROOT / 'data' / 'as.db'
            source_text = input(f'기존 as.db 경로 [기본 {default_source}, 신규 사용은 NEW 입력]: ').strip().strip('"')
            if source_text.upper() != 'NEW':
                source = Path(source_text or default_source)
                if not source.is_file():
                    raise ValueError('기존 as.db를 찾지 못했습니다. 새 DB를 임의로 만들지 않습니다. 기존 데이터 경로를 확인하세요.')
                import_legacy(source, data / 'as.db')
        app.DATA = data
        with app.db(): pass
    config = {'server_pc':socket.gethostname(),'server_url':url.rstrip('/'),'port':port,'data_dir':str(data),'backup_dir':str(backup)}
    temporary = CONFIG.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(CONFIG)
    (ROOT/'AS-client.url').write_text('[InternetShortcut]\nURL=' + config['server_url'] + '\n',encoding='utf-8')
    print('설정 완료. 이 PC: start-server-windows.bat / 다른 PC: AS-client.url 또는 start-client-windows.bat')
    print('접속 주소:',config['server_url'])
    print('Windows 방화벽에서 서버 PC의 Python 또는 TCP 포트를 개인 네트워크에 허용하세요.')


def compact_database(data):
    with storage.ServerLock(data):
        app.DATA = data
        with app.db(): pass
        backup = storage.snapshot(data/'as.db', data/'backups', 'before-compact')
        before = (data/'as.db').stat().st_size
        with closing(sqlite3.connect(data/'as.db', timeout=5)) as c:
            if 'VIRTUAL TABLE' in c.execute("SELECT sql FROM sqlite_master WHERE name='slip_search'").fetchone()[0].upper():
                c.execute("INSERT INTO slip_search(slip_search) VALUES('optimize')")
                c.commit()
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            c.execute('VACUUM')
            c.execute('PRAGMA optimize')
        print(f'정리 완료: {before:,} → {(data/"as.db").stat().st_size:,} bytes. 백업: {backup}')


def main():
    if '--setup' in sys.argv:
        return setup()
    config = read_config()
    if '--server' in sys.argv or '--compact' in sys.argv:
        if not config:
            raise ValueError('먼저 setup-server-windows.bat로 서버를 설정하세요.')
        data = server_data(config)
        if '--compact' in sys.argv:
            return compact_database(data)
        app.DATA = data
        os.environ['AS_HOST'] = '0.0.0.0'
        os.environ['PORT'] = str(config['port'])
        os.environ['AS_BACKUP_DIR'] = config['backup_dir']
        print('다른 PC 접속 주소:',config['server_url'],flush=True)
        print('데이터 저장:',data,flush=True)
        if '--open-browser' not in sys.argv:
            sys.argv.append('--open-browser')
        return app.main()
    if config:
        return app.open_program_window(config['server_url'])
    if '--client' in sys.argv or storage.is_network_path(ROOT):
        raise ValueError('공유 폴더에서는 각 PC가 DB를 직접 열 수 없습니다. 서버로 사용할 PC에서 setup-server-windows.bat를 먼저 실행하세요.')
    # Existing standalone installations on a local disk continue to work.
    if '--open-browser' not in sys.argv:
        sys.argv.append('--open-browser')
    return app.main()


if __name__ == '__main__':
    try:
        main()
    except (ValueError,OSError,sqlite3.Error) as error:
        print(str(error),file=sys.stderr)
        if os.name == 'nt':
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None,str(error),'AS Manager',0x10)
            except OSError:
                pass
        sys.exit(1)
