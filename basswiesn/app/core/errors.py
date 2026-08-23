"""Minimal structured error layer for operational diagnostics."""

from xml.etree import ElementTree as ET

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from basswiesn.app.core.masterlog import write_masterlog


class BasswiesnError(Exception):
    code = "BASSWIESN_ERROR"
    status_code = 500

    def __init__(self, message: str = "BASSWIESN operation failed") -> None:
        super().__init__(message)
        self.message = message


class DeviceUnavailable(BasswiesnError):
    code = "DEVICE_UNREACHABLE"
    status_code = 503


class InvalidXML(BasswiesnError):
    code = "INVALID_XML"
    status_code = 502


class CLICommandFailed(BasswiesnError):
    code = "CLI_COMMAND_FAILED"
    status_code = 502


class UnsafeActionBlocked(BasswiesnError):
    code = "UNSAFE_ACTION_BLOCKED"
    status_code = 409


def error_response(error: BasswiesnError) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={
            "ok": False,
            "error": {"code": error.code, "message": error.message},
        },
    )


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(BasswiesnError)
    async def handle_domain_error(
        _request: Request, error: BasswiesnError
    ) -> JSONResponse:
        write_masterlog("handled_exception", code=error.code, message=error.message)
        return error_response(error)

    @app.exception_handler(httpx.HTTPError)
    async def handle_http_error(
        _request: Request, error: httpx.HTTPError
    ) -> JSONResponse:
        return error_response(DeviceUnavailable(str(error) or "Device is unreachable"))

    @app.exception_handler(ET.ParseError)
    async def handle_xml_error(
        _request: Request, error: ET.ParseError
    ) -> JSONResponse:
        return error_response(InvalidXML(str(error) or "Device returned invalid XML"))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request, error: Exception
    ) -> JSONResponse:
        write_masterlog(
            "unexpected_exception",
            path=request.url.path,
            exception_type=type(error).__name__,
            message=str(error),
        )
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": {"code": "INTERNAL_ERROR", "message": "Unexpected server error"},
            },
        )
