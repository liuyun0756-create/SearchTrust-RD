from uuid import uuid4

import pytest

from tests.integration.support.native_capability import validate_database_url


def test_native_database_accepts_only_the_explicit_loopback_test_identity():
    url=f"postgresql://searchtrust_test@127.0.0.1:54329/searchtrust_v22_test_{uuid4().hex}"
    assert validate_database_url(url,{"SEARCHTRUST_TESTING":"1"}).hostname=="127.0.0.1"
    for changed in (url.replace("127.0.0.1","db.example.com"),url.replace("searchtrust_test@","postgres@"),
        url.replace("/searchtrust_v22_test_","/customer_"),url+"?sslmode=disable",url.replace("searchtrust_test@","searchtrust_test:password@")):
        with pytest.raises(ValueError):validate_database_url(changed,{"SEARCHTRUST_TESTING":"1"})
    for key in ("DATABASE_URL","SUPABASE_DB_URL","RAILWAY_PROJECT_ID","RAILWAY_ENVIRONMENT_ID"):
        with pytest.raises(ValueError):validate_database_url(url,{"SEARCHTRUST_TESTING":"1",key:"configured"})
    with pytest.raises(ValueError):validate_database_url(url,{})
