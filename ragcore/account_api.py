"""Workspace account administration and personal password changes."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, StrictBool

from . import access, accounts


router = APIRouter()


class NewPassword(BaseModel):
    password: str = Field(min_length=12, max_length=1024)


class NewAccount(NewPassword):
    username: str = Field(min_length=1, max_length=64)
    role: Literal["admin", "reader"]


class AccountStatus(BaseModel):
    role: Literal["admin", "reader"]
    enabled: StrictBool


class PersonalPassword(NewPassword):
    current_password: str = Field(min_length=1, max_length=1024)


def change(request, actor, action, username, **fields):
    def authorize(conn):
        latest = access.account_in_transaction(request, conn)
        if latest["owner"] != actor["owner"]:
            raise HTTPException(401, "登录已失效，请重新登录")
        if latest["role"] != actor["role"]:
            raise HTTPException(403, "账号权限已变更")

    try:
        return accounts.manage(request.app.state.store, action, username, actor=access.audit_actor(actor),
                               authorize=authorize, **fields)
    except accounts.AccountConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, "该用户名不存在") from exc


@router.get("/api/admin/accounts")
def list_members(request: Request, actor=Depends(access.admin)):
    return accounts.list_accounts(request.app.state.store)


@router.post("/api/admin/accounts", status_code=201)
def create_member(payload: NewAccount, request: Request, actor=Depends(access.admin)):
    name = change(request, actor, "add", payload.username, password=payload.password, role=payload.role)
    return {"username": name, "role": payload.role, "enabled": True, "login_required": not actor["auth_required"]}


@router.put("/api/admin/accounts/{username}")
def update_member(username: str, payload: AccountStatus, request: Request, actor=Depends(access.admin)):
    name = change(request, actor, "update", username, role=payload.role, enabled=payload.enabled)
    return {"username": name, "role": payload.role, "enabled": payload.enabled}


@router.put("/api/admin/accounts/{username}/password", status_code=204)
def reset_member_password(username: str, payload: NewPassword, request: Request, actor=Depends(access.admin)):
    try:
        is_self = accounts.normalize_username(username) == actor["username"]
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if is_self:
        raise HTTPException(409, "请在个人账号中使用当前密码修改自己的密码")
    change(request, actor, "passwd", username, password=payload.password)


@router.put("/api/account/password", status_code=204)
def change_own_password(payload: PersonalPassword, request: Request, actor=Depends(access.reader)):
    if actor["username"] is None:
        raise HTTPException(403, "只有个人账号可在工作区修改密码")
    change(request, actor, "passwd", actor["username"], password=payload.password,
           current_password=payload.current_password)
    response = Response(status_code=204)
    response.delete_cookie("rag_session", path="/")
    return response
