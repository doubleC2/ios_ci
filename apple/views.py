# Create your views here.
import datetime
import os
import re
import tempfile
import time
from subprocess import call
from typing import Dict, List, Callable, Optional

import gevent
import requests
from django.http import HttpResponseRedirect, HttpResponse, HttpRequest, HttpResponsePermanentRedirect

from base.style import str_json, Assert, json_str, Log, now, Block, date_time_str, Fail, Trace, tran, to_form_url, test_env
from base.utils import base64decode, md5bytes, base64, read_binary_file
from frameworks.base import Action
from frameworks.db import db_model, db_session, message_from_topic
from helper.name_generator import GetRandomName
from .models import IosDeviceInfo, IosAppInfo, IosCertInfo, IosProfileInfo, IosAccountInfo, UserInfo, IosProjectInfo
from .utils import IosAccountHelper, publish_security_code, curl_parse_context, entry, get_capability


def _reg_app(_config: IosAccountInfo, project: str, app_id_id: str, name: str, prefix: str, identifier: str):
    sid = "%s:%s" % (_config.account, project)
    _info = IosAppInfo()
    _info.sid = sid
    _info.app = _config.account
    _info.app_id_id = app_id_id
    _info.identifier = identifier
    _info.name = name
    _info.prefix = prefix
    _info.create = now()
    _info.save()
    Log("注册新的app[%s][%s][%s]" % (_config.account, app_id_id, identifier))


def _reg_cert(_config: IosAccountInfo, cert_req_id, name, cert_id, sn, type_str, expire):
    sid = "%s:%s" % (_config.account, name)
    orig = str_json(db_model.get("IosCertInfo:%s:%s" % (_config.account, name)) or '{}')
    obj = {
        "name": name,
        "app": _config.account,
        "cert_req_id": cert_req_id,
        "cert_id": cert_id,
        "sn": sn,
        "type_str": type_str,
        "expire": expire,
        "expire_str": date_time_str(expire),
    }
    if orig == obj:
        return cert_req_id
    db_model.set("IosCertInfo:%s:%s" % (_config.account, name), json_str(obj), ex=(expire - now()) // 1000)
    _info = IosCertInfo()
    _info.sid = sid
    _info.app = _config.account
    _info.cert_req_id = cert_req_id
    _info.cert_id = cert_id
    _info.sn = sn
    _info.type_str = type_str
    _info.name = name
    _info.create = now()
    _info.expire = datetime.datetime.utcfromtimestamp(expire // 1000)
    _info.save()
    Log("注册新的证书[%s][%s]" % (name, cert_req_id))
    return cert_req_id


def _reg_device(device_id: str, udid: str, model: str, sn: str) -> str:
    # 需要缓存
    _info = IosDeviceInfo()
    _info.udid = udid
    _info.device_id = device_id
    _info.model = model
    _info.sn = sn
    _info.create = now()
    _info.save()
    Log("注册新的设备[%s][%s][%s]" % (udid, device_id, sn))
    return udid


def _get_cert(info: IosAccountInfo) -> IosCertInfo:
    cert = IosCertInfo.objects.filter(
        app=info.account,
        expire__gt=datetime.datetime.utcfromtimestamp(now() // 1000),
        type_str="development",
    ).first()  # type: IosCertInfo
    if not cert:
        # todo: 生成证书
        pass
    return Assert(cert, "缺少现成的开发[iOS App Development]证书[%s]" % info.account)


def _get_app(_config: IosAccountHelper, project: str) -> IosAppInfo:
    app = IosAppInfo.objects.filter(
        sid="%s:%s" % (_config.account, project),
    ).first()  # type: IosAppInfo
    if not app:
        _project = IosProjectInfo.objects.filter(project=project).first()  # type: IosProjectInfo
        _identifier = "%s.%s" % (_project.bundle_prefix, GetRandomName())
        _config.post(
            "验证一个app",
            "https://developer.apple.com/services-account/QH65B2/account/ios/identifiers/validateAppId.action?teamId=",
            data={
                "teamId": _config.team_id,
                "appIdName": "ci%s" % project,
                "appIdentifierString": _identifier,
                "type": "explicit",
                "explicitIdentifier": _identifier,
            },
        )
        _config.post(
            "注册一个app",
            "https://developer.apple.com/services-account/v1/bundleIds",
            data=json_str({
                "data": {
                    "type": "bundleIds",
                    "attributes": {
                        "name": "ci%s" % project,
                        "identifier": _identifier,
                        "platform": "IOS",
                        "seedId": _config.team_id,
                        "teamId": _config.team_id,
                    },
                    "relationships": {
                        "bundleIdCapabilities": {
                            "data": tran(get_capability, str_json(_project.capability)),
                        },
                    },
                },
            }),
            ex_headers={
                "content-type": "application/vnd.api+json",
            },
            csrf=True,
            json_api=False,
            status=201,
        )
        __list_all_app(_config, project)
        app = IosAppInfo.objects.filter(
            sid="%s:%s" % (_config.account, project),
        ).first()  # type: IosAppInfo
    return Assert(app, "账号[%s]缺少app[%s]" % (_config.account, project))


def _get_device_id(udid_list: List[str]) -> Dict[str, str]:
    return dict(
        zip(udid_list,
            map(lambda x: str_json(x)["device_id"] if x else x,
                db_model.mget(list(map(lambda x: "IosDeviceInfo:%s" % x, udid_list)))
                )
            )
    )


def __list_all_app(_config: IosAccountHelper, project: str):
    ret = _config.post(
        "所有的app",
        "https://developer.apple.com/services-account/QH65B2/account/ios/identifiers/listAppIds.action?teamId=",
        data={
            "pageNumber": 1,
            "pageSize": 500,
            "sort": "name%3dasc",
            "onlyCountLists": True,
        })
    for app in ret["appIds"]:  # type: Dict
        if app["name"] != "ci%s" % project:
            continue
        _reg_app(_config.info, project, app["appIdId"], app["name"], app["prefix"], app["identifier"])


def _to_ts(date_str: str):
    return int(time.mktime(time.strptime(date_str.replace("Z", "UTC"), '%Y-%m-%dT%H:%M:%S%Z')) * 1000)


def _to_dt(date_str: str):
    return datetime.datetime.utcfromtimestamp(_to_ts(date_str) // 1000)


def __download_profile(_config: IosAccountHelper, _profile: IosProfileInfo):
    ret = _config.post(
        "获取profile文件",
        "https://developer.apple.com/services-account/QH65B2/account/ios/profile/downloadProfileContent?teamId=",
        data={
            "provisioningProfileId": _profile.profile_id,
        },
        json_api=False,
        is_json=False,
        log=False,
        is_binary=True,
        method="GET",
    )
    profile = base64(ret)
    if profile != _profile.profile:
        _profile.profile = base64(ret)
        _profile.save()
        Log("更新profile文件[%s]" % _profile.sid)


def __profile_detail(_config: IosAccountHelper, _profile: IosProfileInfo):
    ret = _config.post(
        "获取profile详情",
        "https://developer.apple.com/services-account/QH65B2/account/ios/profile/getProvisioningProfile.action?teamId=",
        data={
            "includeInactiveProfiles": True,
            "provisioningProfileId": _profile.profile_id,
            "teamId": _config.team_id,
        },
    )
    profile = ret["provisioningProfile"]
    devices = list(map(lambda x: x["deviceNumber"], profile["devices"]))
    devices_str = json_str(devices)
    if _profile.devices != devices_str:
        _profile.devices = devices_str
        _profile.devices_num = len(devices)
        __download_profile(_config, _profile)
        Log("更新profile[%s]" % _profile.sid)
        _profile.save()


def __list_all_profile(_config: IosAccountHelper, target_project: str = ""):
    ret = _config.post(
        "更新列表",
        "https://developer.apple.com/services-account/QH65B2/account/ios/profile/listProvisioningProfiles.action?teamId=",
        data={
            "includeInactiveProfiles": True,
            "onlyCountLists": True,
            "sidx": "name",
            "sort": "name%3dasc",
            "teamId": _config.team_id,
            "pageNumber": 1,
            "pageSize": 500,
        })
    target = None
    for profile in ret["provisioningProfiles"]:
        if not profile["name"].startswith("专用 "):
            continue
        project = profile["name"].replace("专用 ", "")
        _info = IosProfileInfo.objects.filter(sid="%s:%s" % (_config.account, project)).first()  # type: IosProfileInfo
        expire = _to_dt(profile["dateExpire"])
        detail = False
        if not _info:
            _info = IosProfileInfo()
            _info.sid = "%s:%s" % (_config.account, project)
            _info.app = _config.account
            _info.profile_id = profile["provisioningProfileId"]
            _info.expire = expire
            _info.devices = ""
            _info.devices_num = 0
            detail = True

        if _info.expire != expire:
            _info.profile_id = profile["provisioningProfileId"]
            _info.expire = expire
            detail = True

        if detail:
            # 获取细节
            __profile_detail(_config, _info)
            Log("更新profile[%s]" % _info.sid)
            _info.save()
        if project == target_project:
            target = _info
    return ret, target


def __list_all_cert(_config: IosAccountHelper):
    ret = _config.post(
        "所有的证书",
        "https://developer.apple.com/services-account/QH65B2/account/ios/certificate/listCertRequests.action?teamId=",
        data={
            "pageNumber": 1,
            "pageSize": 500,
            "sort": "certRequestStatusCode%3dasc",
            "certificateStatus": 0,
            "types": "5QPB9NHCEI",  # 证书类型
        })
    for cert in ret["certRequests"]:  # type: Dict
        _reg_cert(
            _config.info,
            cert["certRequestId"],
            cert["name"],
            cert["certificateId"],
            cert["serialNum"],
            cert["certificateType"]["permissionType"],
            _to_ts(cert["expirationDate"]),
        )


@Action
def init_account(account: str):
    _config = IosAccountHelper(IosAccountInfo.objects.filter(account=account).first())
    __list_all_devices(_config)
    __list_all_app(_config, "")
    __list_all_cert(_config)
    __list_all_profile(_config)
    return {
        "succ": True,
    }


@Action
def download_profile(uuid: str):
    """
    基于用户id下载
    """
    _user = UserInfo.objects.filter(uuid=uuid).first()  # type: UserInfo
    Assert(_user is not None, "没有找到uuid[%s]" % uuid)
    _config = IosAccountHelper(IosAccountInfo.objects.filter(account=_user.account).first())
    _info = IosProfileInfo.objects.filter(sid="%s" % _config.account).first()  # type: IosProfileInfo
    return {
        "encodedProfile": _info.profile,
    }


def _newbee(_project: IosProjectInfo):
    # 默认一天的时效
    for _ in range(100):
        import uuid
        _uuid = uuid.uuid4()
        if db_session.set("uuid:%s" % _uuid, json_str({
            "project": _project.project,
        }), ex=3600 * 24, nx=True):
            return str(_uuid)
    raise Fail("生成失败")


@Action
def newbee(project: str):
    """
    根据项目生成具体的一个可以注册新设备的uuid
    """
    _info = IosProjectInfo.objects.filter(project=project).first()  # type: IosProjectInfo
    Assert(_info is not None, "找不到对应的项目[%s]" % project)
    return {
        "uuid": _newbee(_info),
    }


def __fetch_account(udid: str, project: str, action: Callable[[IosAccountInfo, str, str], bool]) -> IosAccountInfo:
    """
    循环使用所有的账号
    """
    for each in IosAccountInfo.objects.filter(devices_num__lt=100).order_by("-devices_num"):
        if action(each, udid, project):
            return each
    raise Fail("没有合适的账号了")


def __list_all_devices(_config: IosAccountHelper):
    ret = _config.post(
        "获取所有的列表",
        "https://developer.apple.com/services-account/QH65B2/account/ios/device/listDevices.action?teamId=",
        data={
            "includeRemovedDevices": True,
            "includeAvailability": True,
            "pageNumber": 1,
            "pageSize": 100,
            "sort": "status%3dasc",
            "teamId": _config.team_id,
        }, log=False)
    for device in ret["devices"]:  # type: Dict
        _reg_device(device["deviceId"],
                    device["deviceNumber"],
                    device.get("model", device.get("deviceClass", "#UNKNOWN#")),
                    device.get("serialNumber", "#UNKNOWN#"))
    # 更新一下info
    devices = list(map(lambda x: x["deviceNumber"], ret["devices"]))
    if json_str(devices) != _config.info.devices:
        Log("更新设备列表[%s]数量[%s]=>[%s]" % (_config.account, _config.info.devices_num, len(devices)))
        _config.info.devices = json_str(devices)
        _config.info.devices_num = len(devices)
        _config.info.save()


def __add_device(account: IosAccountInfo, udid: str, project: str) -> bool:
    title = "设备%s" % udid
    _config = IosAccountHelper(account)
    try:
        _device = IosDeviceInfo.objects.filter(udid=udid).first()  # type:Optional[IosDeviceInfo]
        if not _device:
            # 先注册设备
            ret = _config.post(
                "验证设备udid",
                "https://developer.apple.com/services-account/QH65B2/account/ios/device/validateDevices.action?teamId=", {
                    "deviceNames": title,
                    "deviceNumbers": udid,
                    "register": "single",
                    "teamId": _config.team_id,
                }, cache=True)

            Assert(len(ret["failedDevices"]) == 0, "验证udid请求失败[%s][%s]" % (udid, ret["validationMessages"]))
            __list_all_devices(_config)
            ret = _config.post(
                "添加设备",
                "https://developer.apple.com/services-account/QH65B2/account/ios/device/addDevices.action?teamId=%s" % _config.team_id, {
                    "deviceClasses": "iphone",
                    "deviceNames": title,
                    "deviceNumbers": udid,
                    "register": "single",
                    "teamId": _config.team_id,
                }, csrf=True)
            Assert(ret["resultCode"] == 0, "添加udid请求失败[%s]" % udid)
            Assert(not ret["validationMessages"], "添加udid请求失败[%s]" % udid)
            Assert(ret["devices"], "添加udid请求失败[%s]" % udid)
            device = ret["devices"][0]  # type: Dict
            _reg_device(device["deviceId"], device["deviceNumber"], device["model"], device["serialNumber"])

        with Block("更新"):
            ret, _info = __list_all_profile(_config, project)
            if not _info:
                _info = IosProfileInfo()
                _info.sid = "%s:%s" % (_config.account, project)
                _info.app = _config.account
                _info.devices = "[]"
                _info.devices_num = 0
                _info.project = project

            devices = str_json(_info.devices)  # type: List[str]

            if udid in devices:
                pass
            else:
                devices.append(udid)
                with Block("默认全开当期的设备"):
                    # noinspection PyTypeChecker
                    devices = list(set(devices + str_json(_config.info.devices)))
                _cert = _get_cert(_config.info)
                _app = _get_app(_config, project)
                found = False
                for each in ret["provisioningProfiles"]:  # type: Dict
                    if each["name"] != "专用 %s" % project:
                        continue
                    # todo: 过期更新
                    ret = _config.post(
                        "更新ProvisioningProfile",
                        "https://developer.apple.com/services-account/QH65B2/account/ios/profile/regenProvisioningProfile.action?teamId=",
                        data={
                            "provisioningProfileId": each["provisioningProfileId"],
                            "distributionType": "limited",
                            "subPlatform": "",
                            "returnFullObjects": False,
                            "provisioningProfileName": each["name"],
                            "appIdId": _app.app_id_id,
                            "certificateIds": _cert.cert_req_id,
                            "deviceIds": ",".join(_get_device_id(devices).values()),
                        }, csrf=True)
                    Assert(ret["resultCode"] == 0)
                    _info.devices = json_str(devices)
                    _info.profile_id = each["provisioningProfileId"]
                    # noinspection PyTypeChecker
                    _info.profile = ret["provisioningProfile"]["encodedProfile"]
                    _info.expire = _to_dt(ret["provisioningProfile"]["dateExpire"])
                    _info.save()
                    found = True
                    Log("更新证书[%s]添加设备[%s][%s]成功" % (project, udid, len(devices)))
                    break
                if not found:
                    ret = _config.post(
                        "创建ProvisioningProfile",
                        "https://developer.apple.com/services-account/QH65B2/account/ios/profile/createProvisioningProfile.action?teamId=",
                        data={
                            "subPlatform": "",
                            "certificateIds": _cert.cert_req_id,
                            "deviceIds": ",".join(_get_device_id(devices).values()),
                            "template": "",
                            "returnFullObjects": False,
                            "distributionTypeLabel": "distributionTypeLabel",
                            "distributionType": "limited",
                            "appIdId": _app.app_id_id,
                            "appIdName": _app.name,
                            "appIdPrefix": _app.prefix,
                            "appIdIdentifier": _app.identifier,
                            "provisioningProfileName": "专用 %s" % project,
                        }, csrf=True)
                    Assert(ret["resultCode"] == 0)
                    # noinspection PyTypeChecker
                    _info.profile_id = ret["provisioningProfile"]["provisioningProfileId"]
                    # noinspection PyTypeChecker
                    _info.profile = ret["provisioningProfile"]["encodedProfile"]
                    _info.expire = _to_dt(ret["provisioningProfile"]["dateExpire"])
                    _info.save()
                    Log("添加证书[%s]添加设备[%s][%s]成功" % (project, udid, len(devices)))
    except Exception as e:
        Trace("添加设备出错了[%s]" % e, e)
        return False
    return True


def __add_task(_user: UserInfo):
    _account = IosAccountInfo.objects.filter(account=_user.account).first()  # type:IosAccountInfo
    _project = IosProjectInfo.objects.filter(project=_user.project).first()  # type:IosProjectInfo
    _profile = IosProfileInfo.objects.filter(sid="%s:%s" % (_user.account, _user.project)).first()  # type:IosProfileInfo
    Assert(_profile, "[%s][%s]证书无效" % (_project.project, _account.account))
    Log("[%s]发布任务[%s]" % (_user.project, _user.account))
    db_session.publish("task:package", json_str({
        "cert": "iPhone Developer: zhangming luo",
        "cert_p12": "",
        "mp_url": entry("/apple/download_mp?uuid=%s" % _user.uuid),
        "mp_md5": md5bytes(base64decode(_profile.profile)),
        "project": _project.project,
        "ipa_url": _asset_url("%s/orig.ipa" % _user.project),
        "ipa_md5": _project.md5sum,
        "ipa_new": "%s_%s.ipa" % (_account.team_id, _account.devices_num),
        "upload_url": entry("/apple/upload_ipa?project=%s&account=%s" % (_user.project, _user.account)),
        "ts": now(),
    }))


# noinspection PyShadowingNames
@Action
def add_device(_content: bytes, uuid: str, udid: str = ""):
    _key = "uuid:%s" % uuid
    _detail = str_json(db_session.get(_key) or "{}")
    _account = _detail.get("account")
    project = _detail["project"]
    if not _detail:
        raise Fail("无效的uuid[%s]" % uuid)
    if not udid:
        # todo: 验证来源
        with Block("提取udid", fail=False):
            udid = re.compile(br"<key>UDID</key>\n\s+<string>(\w+)</string>").findall(_content)[0].decode("utf8")
    if not udid:
        # 提取udid失败后删除uuid
        db_session.delete(_key)
        return {
            "succ": False,
        }
    for _user in UserInfo.objects.filter(udid=udid):
        _account = _user.account
        if _user.project == _detail["project"]:
            if uuid != _user.uuid:
                Log("转移设备的[%s]的uuid[%s]=>[%s]" % (udid, uuid, _user.uuid))
                uuid = _user.uuid
                break

    if not _account:
        Log("为设备[%s]分配账号" % udid)
        _account = __fetch_account(udid, project, __add_device)
    else:
        _account = IosAccountInfo.objects.filter(account=_account).first()

    _user = UserInfo(uuid=uuid)
    _user.udid = udid
    _user.project = _detail["project"]
    _user.account = _account.account
    _user.save()
    if test_env():
        # 测试环境可能uuid要重复测
        pass
    else:
        db_session.delete(_key)
    __add_task(_user)
    return HttpResponsePermanentRedirect("https://iosstore.sklxsj.com/detail.php?project=%s" % project)


@Action
def login_by_fastlane(_req: HttpRequest, cmd: str = "", account: str = ""):
    """
    通过命令获取会话
    fastlane spaceauth -u kitsudo163@163.com
    ex.
    export FASTLANE_SESSION='---\n- !ruby/object:HTTP::Cookie\n  name: myacinfo\n  value: DAWTKNV2e32a0823580640561dc9dfd382265048c32c2aa5b04485930b2ada1d1c7eba28dee6c6065c749f708f2a429f8f9f2d0f2f7d2ad629652ca5bc3383c0772d51c6ca34a2f7b09141f7b19c358c2b25d0738079ab1e48a06610228a574342c84ef13349ede1a012c25155e265a17989a3b09631dd953954505153fb7ef71aecfe303530cb684c89e8402cb82d8d4d93c3fc3c1209e334f65f71c7ae0cfdf0349ec757abcb104a591f5bea25ac0f1207004c19645b80ed82fb5cd0d3a740224b2f3aef9e91b049bb63a94ae3c76d027411f077069865209d733617a7a84f54cf7e9488e9b4f0a918d29f184f5ec76d95b5f55def61682f70b7f10fc12dc43d6e380213dd1f702a4f3ccab3ad438cd0f6a87c295e028a12ec410aa3fa689210d040377995914d4d3718b90f85ad5452d5db47ef9ae11c6b3216cf8ab61025adc203b0bf072ce832240c384d83f0f4aaf477a3c7313de4c20c5e32c530ff1ad76aebcd8538ac485a9a46941dfa94ee2f3fb40e38666533326562333665333834343461323366383636666563303166613533303330656361323836MVRYV2\n  domain: apple.com\n  for_domain: true\n  path: "/"\n  secure: true\n  httponly: true\n  expires: \n  max_age: \n  created_at: 2019-03-15 11:55:51.031441000 +08:00\n  accessed_at: 2019-03-15 11:55:51.041509000 +08:00\n- !ruby/object:HTTP::Cookie\n  name: dqsid\n  value: eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJqdGkiOiJzZnp2ZGFldGJPeWpXaTc1LVpTRmNBIn0.IEYqXF-pxIYdIwP2rdNRNhoxdCzJgGxt4olTZa2fXo8\n  domain: olympus.itunes.apple.com\n  for_domain: false\n  path: "/"\n  secure: true\n  httponly: true\n  expires: \n  max_age: 1800\n  created_at: &1 2019-03-15 11:55:52.798977000 +08:00\n  accessed_at: *1\n
    curl localhost:8000/apple/login_by_fastlane --data $FASTLANE_SESSION
    """
    if not cmd:
        cmd = _req.body.decode("utf-8")
    Assert(len(cmd), "命令行不对")
    cmd = cmd.replace("\\n", "")
    cookie = dict(re.compile(r"name:\s+(\S+)\s+value:\s+(\S+)").findall(cmd))
    if not account:
        rsp = requests.post("https://developer.apple.com/services-account/QH65B2/account/getUserProfile", headers={
            "cookie": to_form_url(cookie, split=';')
        })
        if rsp.status_code != 200:
            return {
                "succ": False,
                "reason": "无效的fastlane export",
            }
        data = rsp.json()
        account = data["userProfile"]["email"]

    if account:
        _info = IosAccountInfo.objects.filter(account=account).first()  # type:IosAccountInfo
        if not _info:
            _info = IosAccountInfo()
            _info.account = account
            _info.save()
        _info.cookie = json_str(cookie)
        _info.headers = json_str({})
        _info.save()
        Log("通过fastlane登录[%s]" % account)
        return {
            "succ": True,
            "msg": "登录[%s]成功" % account,
        }
    else:
        return {
            "succ": False,
            "msg": "请求不具备提取登录信息",
        }


@Action
def login_by_curl(_req: HttpRequest, cmd: str = "", account: str = ""):
    """
    通过拦截网页的curl 请求
    https://developer.apple.com/account/getUserProfile
    ex.
    curl 'https://developer.apple.com/services-account/QH65B2/account/getUserProfile' -H 'origin: https://developer.apple.com' -H 'accept-encoding: gzip, deflate, br' -H 'accept-language: zh-CN,zh;q=0.9' -H 'csrf: cf0796aee015fe0f03e7ccc656ba4b898b696cc1072027988d89b1f6e607fd67' -H 'cookie: geo=SG; ccl=SR+vWVA/vYTrzR1LkZE2tw==; s_fid=56EE3E795513A2B4-16F81B5466B36881; s_cc=true; s_vi=[CS]v1|2E425B0B852E2C90-40002C5160000006[CE]; dslang=CN-ZH; site=CHN; s_pathLength=developer%3D2%2C; acn01=v+zxzKnMyleYWzjWuNuW1Y9+kAJBxfozY2UAH0paNQB+FA==; myacinfo=DAWTKNV2a5c238e8d27e8ed221c8978cfb02ea94b22777f25ffec5abb1a855da8debe4f59d60b506eae457dec4670d5ca9663ed72c3d1976a9f87c53653fae7c63699abe64991180d7c107c50ce88be233047fc96de200c3f23947bfbf2e064c7b9a7652002d285127345fe15adf53bab3d347704cbc0a8b856338680722e5d0387a5eb763d258cf19b79318be28c4abd01e27029d2ef26a1bd0dff61d141380a1b496b92825575735d0be3dd02a934db2d788c9d6532b6a36bc69d244cc9b4873cef8f4a3a90695f172f6f521330f67f20791fd7d62dfc9d6de43899ec26a8485191d62e2c5139f81fca2388d57374ff31f9f689ad373508bcd74975ddd3d3b7875fe3235323962636433633833653433363562313034383164333833643736393763303538353038396439MVRYV2; DSESSIONID=1c3smahkpfbkp7k3fju30279uoba8p8480gs5ajjgsbbvn8lubqt; s_sq=%5B%5BB%5D%5D' -H 'user-locale: en_US' -H 'user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/69.0.3497.81 Safari/537.36 QQBrowser/4.5.122.400' -H 'content-type: application/json' -H 'accept: application/json' -H 'cache-control: max-age=0, no-cache, no-store, must-revalidate, proxy-revalidate' -H 'authority: developer.apple.com' -H 'referer: https://developer.apple.com/account/' -H 'csrf_ts: 1552204197631' --data-binary '{}' --compressed
    """

    if not cmd:
        cmd = _req.body.decode("utf-8")
    Assert(len(cmd) and cmd.startswith("curl"), "命令行不对")
    parsed_context = curl_parse_context(cmd)
    params = {
        "data": parsed_context.data,
        "headers": dict(filter(lambda x: not x[0].startswith(":"), parsed_context.headers.items())),
        "cookies": parsed_context.cookies,
    }
    if parsed_context.method == 'get':
        rsp = requests.get(
            parsed_context.url,
            **params,
        )
    else:
        rsp = requests.post(
            parsed_context.url,
            **params,
        )
    if rsp.status_code != 200:
        return {
            "succ": False,
            "reason": "无效的curl",
        }
    if parsed_context.url != "https://developer.apple.com/services-account/QH65B2/account/getUserProfile":
        data = rsp.json()
        account = data["userProfile"]["email"]
    else:
        rsp = requests.post("https://developer.apple.com/services-account/QH65B2/account/getUserProfile", headers={
            "cookie": parsed_context.cookies,
        })
        data = rsp.json()
        account = data["userProfile"]["email"]

    if account:
        _info = IosAccountInfo.objects.filter(account=account).first()  # type:IosAccountInfo
        if not _info:
            _info = IosAccountInfo()
            _info.account = account
            _info.save()
        _info.cookie = json_str(parsed_context.cookies)
        _info.headers = json_str(parsed_context.headers)
        _info.save()
        Log("通过curl登录[%s]" % account)
        return {
            "succ": True,
            "msg": "登录[%s]成功" % account,
        }
    else:
        return {
            "succ": False,
            "msg": "请求不具备提取登录信息",
        }


@Action
def upload_ipa(project: str, account: str, file: bytes):
    base = os.path.join("static/income", project)
    os.makedirs(base, exist_ok=True)
    _info = IosAccountInfo.objects.filter(account=account).first()  # type:IosAccountInfo
    Assert(_info, "账号[%s]不存在" % account)
    filename = "%s_%s.ipa" % (_info.team_id, _info.devices_num)
    with open(os.path.join(base, filename), mode="wb") as fout:
        fout.write(file)
    Log("[%s]收到新打包的ipa[%s]" % (account, filename))
    return {
        "succ": True,
    }


def _asset_url(path):
    return entry("/income/" + path)


# noinspection PyShadowingNames
@Action
def download_ipa(uuid: str):
    _user = UserInfo.objects.filter(uuid=uuid).first()  # type: UserInfo
    _info = IosAccountInfo.objects.filter(account=_user.account).first()  # type:IosAccountInfo
    return HttpResponseRedirect(_asset_url("%s/%s_%s.ipa" % (_user.project, _info.team_id, _info.devices_num)))


@Action
def download_mp(uuid: str, filename: str = "package.mobileprovision"):
    _user = UserInfo.objects.filter(uuid=uuid).first()  # type: UserInfo
    _info = IosAccountInfo.objects.filter(account=_user.account).first()  # type:IosAccountInfo
    _profile = IosProfileInfo.objects.filter(sid="%s:%s" % (_user.account, _user.project)).first()  # type:IosProfileInfo
    response = HttpResponse(base64decode(_profile.profile))
    response['Content-Type'] = 'application/octet-stream'
    response['Content-Disposition'] = 'attachment;filename="%s"' % filename
    return response


# noinspection SpellCheckingInspection
@Action
def mobconf(uuid: str = ""):
    """
    下载config实现udid的上传
    """
    if uuid:
        orig = read_binary_file("mdmtools/mdm.mobileconfig").replace(b"xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", uuid.encode("utf8"))
        _, in_path = tempfile.mkstemp()
        _, out_path = tempfile.mkstemp()
        try:
            # todo: 缓存uuid的mobileconfig文件
            with open(in_path, mode="wb") as fout:
                fout.write(orig)
            assert call(
                ["openssl", "smime", "-sign", "-in", in_path, "-out", out_path, "-signer", "mdmtools/mobconf/InnovCertificates.pem",
                 "-inkey",
                 "mdmtools/mobconf/key.pem", "-certfile", "mdmtools/mobconf/root.crt.pem", "-outform", "der", "-nodetach", "-passin",
                 "pass:123123"], timeout=10) == 0
            rsp = HttpResponse(read_binary_file(out_path))
            rsp["Content-Type"] = "application/x-apple-aspen-config; charset=utf-8"
            rsp["Content-Disposition"] = 'attachment; filename="ioshelper.mobileconfig"'
            return rsp
        finally:
            os.remove(in_path)
            os.remove(out_path)
    else:
        # 走固定的安装之后确认uuid
        orig = read_binary_file("mdmtools/mdm_signed.mobileconfig")
        rsp = HttpResponse(orig)
        rsp["Content-Type"] = "application/x-apple-aspen-config; charset=utf-8"
        rsp["Content-Disposition"] = 'attachment; filename="ioshelper.mobileconfig"'
        return rsp


@Action
def wait():
    __pub = db_session.pubsub()
    __pub.subscribe("account:security:code")
    expire = now() + 1200 * 1000
    while now() < expire:
        gevent.sleep(1)
        for data in message_from_topic(__pub, is_json=True, limit=1):
            return data
    raise Fail("超时")


@Action
def info(project: str):
    _project = IosProjectInfo.objects.filter(project=project).first()  # type: IosProjectInfo
    if _project:
        ret = str_json(_project.comments)
        ret.update({
            "uuid": _newbee(_project),
        })
        return ret
    return {

    }


@Action
def get_ci():
    pass


@Action
def security_code_sms(phone: str, sms: str):
    Assert("apple" in sms.lower(), "短信非验证码短信[%s]" % sms)
    code = re.compile(r"\d{4,6}").findall(sms)
    Assert(len(code), "短信非验证码短信[%s]" % sms)
    code = code[0]
    _account = IosAccountInfo.objects.filter(phone=phone).first()  # type: IosAccountInfo
    publish_security_code(_account.account if _account else "*", code, now())
    return {
        "succ": True,
    }


@Action
def security_code(account: str, code: str):
    publish_security_code(account, code, now())
    return {
        "succ": True,
    }
