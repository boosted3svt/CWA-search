import json
import pytest
from config import Settings
from calibre.client import Client, Library
from conftest import Session, Response

@pytest.mark.parametrize('verify', [True, False])
def test_global_setting_applies_to_every_configured_library(tmp_path, verify):
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'verify_tls':verify}))
    (tmp_path/'libraries.txt').write_text('https://one.test\nhttps://two.test:8443\n')
    settings=Settings.load(config)
    for _, library in settings.libraries():
        session=Session([Response(b'')])
        response,_=Client(library,session=session).get(library.url+'/opds')
        response.close()
        assert session.calls[0][1]['verify'] is verify


def test_tls_verification_enabled_by_default(tmp_path):
    settings=Settings.load(tmp_path/'config.json')
    assert settings.library('https://example.test').verify_tls is True


def test_exception_not_extended_to_additional_redirect_hosts():
    session=Session([Response(b'',status=302,headers={'Location':'https://other.test/book'}),Response(b'')])
    library=Library('https://one.test',allowed_hosts=['other.test'],verify_tls=False)
    Client(library,session=session).get('https://one.test/book')
    assert session.calls[0][1]['verify'] is False
    assert session.calls[1][1]['verify'] is True


@pytest.mark.parametrize('value', ['false',0,None])
def test_setting_must_be_boolean(tmp_path,value):
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'verify_tls':value}))
    with pytest.raises(ValueError,match='verify_tls'):
        Settings.load(config)
