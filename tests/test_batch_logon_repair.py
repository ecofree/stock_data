import pytest

from tools.v2.repair_batch_logon import add_only_batch, EXPECTED_SID, RIGHT, policy_state, no_entries


class Policy:
    def __init__(self):
        self.allow={'existing'};self.deny=set();self.rights={'unrelated_right'};self.calls=[]
    def ConvertSidToStringSid(self,sid):return sid
    def LsaEnumerateAccountsWithUserRight(self,handle,right):return self.allow if right==RIGHT else self.deny
    def LsaEnumerateAccountRights(self,handle,sid):return self.rights
    def LsaAddAccountRights(self,handle,sid,rights):
        self.calls.append((sid,rights));self.allow.add(sid);self.rights.update(rights)


def test_grants_exactly_one_right_preserving_every_existing_principal():
    api=Policy();before=policy_state(api,None,EXPECTED_SID)
    result=add_only_batch(api,None,EXPECTED_SID,before)
    assert api.calls==[(EXPECTED_SID,(RIGHT,))]
    assert result['allow']==sorted({'existing',EXPECTED_SID})
    assert set(result['account_rights'])=={'unrelated_right',RIGHT}
    add_only_batch(api,None,EXPECTED_SID,result)
    assert len(api.calls)==1


def test_unknown_sid_or_deny_policy_never_changes_rights():
    api=Policy();before=policy_state(api,None,EXPECTED_SID)
    with pytest.raises(ValueError):add_only_batch(api,None,'unreviewed',before)
    api.deny.add('unrelated_deny_group')
    with pytest.raises(ValueError):add_only_batch(api,None,EXPECTED_SID,policy_state(api,None,EXPECTED_SID))
    assert api.calls==[]


def test_native_missing_account_rights_not_confused_with_access_denied():
    class NativeError(Exception):
        def __init__(self,code):self.winerror=code
    def fail(code):raise NativeError(code)
    assert no_entries(fail,2)==()
    assert no_entries(fail,259)==()
    with pytest.raises(NativeError):no_entries(fail,5)
