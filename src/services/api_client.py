"""
Cliente HTTP para interação com a API do backend Grava Nois.

Centraliza todas as chamadas HTTP (registro de metadados, upload para URL assinada,
finalização de upload) em uma classe reutilizável.
"""

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import requests

from src.security.request_signer import SignedRequest, sign_request, truncate_signature
from src.utils.logger import logger

_METADATA_SIGNED_PATH_RE = re.compile(r"^/api/videos/metadados/client/([^/]+)(?:/|$)")
_UPLOADED_SIGNED_PATH_RE = re.compile(r"^/api/videos/[^/]+/uploaded$")


class GravaNoisAPIClient:
    """
    Cliente HTTP para comunicação com o backend Grava Nois.

    Gerencia autenticação, timeouts e tratamento de erros para todas as operações
    de registro e upload de clipes.
    """

    def __init__(
        self,
        api_base: Optional[str] = None,
        api_token: Optional[str] = None,
        client_id: Optional[str] = None,
        venue_id: Optional[str] = None,
        default_timeout: float = 10.0,
    ):
        """
        Inicializa o cliente de API.

        Args:
            api_base: URL base da API (ex: https://api.gravanois.com). Se None, lê de env.
            api_token: Token de autenticação. Se None, lê de env.
            client_id: ID do cliente. Se None, lê de env.
            venue_id: ID do local (venue). Se None, lê de env.
            default_timeout: Timeout padrão para requisições HTTP (segundos)
        """
        self.api_base = (
            api_base or os.getenv("GN_API_BASE") or os.getenv("API_BASE_URL") or ""
        ).rstrip("/")
        self.api_token = api_token or os.getenv("GN_API_TOKEN") or os.getenv("API_TOKEN")
        self.client_id = client_id or os.getenv("GN_CLIENT_ID") or os.getenv("CLIENT_ID")
        self.venue_id = venue_id or os.getenv("GN_VENUE_ID") or os.getenv("VENUE_ID")
        self.device_id = os.getenv("DEVICE_ID") or os.getenv("GN_DEVICE_ID") or ""
        self.device_secret = (
            os.getenv("DEVICE_SECRET") or os.getenv("GN_DEVICE_SECRET") or ""
        )
        self.hmac_dry_run = self._is_truthy(
            os.getenv("GN_HMAC_DRY_RUN") or os.getenv("HMAC_DRY_RUN")
        )
        self.default_timeout = default_timeout

        if not self.api_base:
            logger.warning("API base URL não configurada (GN_API_BASE ou API_BASE_URL)")

    @staticmethod
    def _is_truthy(value: Optional[str]) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _extract_path(url: str) -> str:
        parsed = urlparse(url)
        return parsed.path or "/"

    @staticmethod
    def _is_hmac_protected_path(path: str) -> bool:
        return bool(
            _METADATA_SIGNED_PATH_RE.match(path) or _UPLOADED_SIGNED_PATH_RE.match(path)
        )

    @staticmethod
    def _extract_client_id_from_path(path: str) -> Optional[str]:
        match = _METADATA_SIGNED_PATH_RE.match(path)
        if not match:
            return None
        return match.group(1)

    @staticmethod
    def _safe_headers_for_log(headers: Dict[str, str]) -> Dict[str, str]:
        safe = dict(headers)
        signature = safe.get("X-Signature")
        if signature:
            safe["X-Signature"] = truncate_signature(signature)
        auth = safe.get("Authorization")
        if auth:
            safe["Authorization"] = "Bearer ***"
        return safe

    def _build_signed_headers(self, *, path: str, body_string: str) -> SignedRequest:
        if not self.device_id:
            raise RuntimeError("DEVICE_ID não configurado para assinatura HMAC")
        if not self.device_secret:
            raise RuntimeError("DEVICE_SECRET não configurado para assinatura HMAC")

        path_client_id = self._extract_client_id_from_path(path)
        resolved_client_id = self.client_id or path_client_id
        if path_client_id and resolved_client_id != path_client_id:
            raise RuntimeError(
                "client_id inconsistente: path e configuração do device divergem"
            )
        if not resolved_client_id:
            raise RuntimeError("CLIENT_ID não configurado para assinatura HMAC")

        return sign_request(
            method="POST",
            path=path,
            body_string=body_string,
            device_id=self.device_id,
            device_secret=self.device_secret,
            client_id=resolved_client_id,
            content_type="application/json",
        )

    def is_configured(self) -> bool:
        """Verifica se o cliente está configurado com uma URL base."""
        return bool(self.api_base)

    def _http_post_json(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Envia requisição HTTP POST com payload JSON.

        Args:
            url: URL completa do endpoint
            payload: Dados a enviar como JSON
            headers: Headers HTTP adicionais
            timeout: Timeout em segundos (usa default_timeout se None)

        Returns:
            Resposta JSON do servidor

        Raises:
            RuntimeError: Em caso de erro HTTP ou de rede
        """
        timeout = timeout or self.default_timeout
        headers = dict(headers or {})
        body_string = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        path = self._extract_path(url)
        signed_req: Optional[SignedRequest] = None

        try:
            if self._is_hmac_protected_path(path):
                signed_req = self._build_signed_headers(path=path, body_string=body_string)
                headers.update(signed_req.headers)

                if self.hmac_dry_run:
                    safe_headers = self._safe_headers_for_log(headers)
                    logger.info("[HMAC DRY-RUN] POST %s", path)
                    logger.info("[HMAC DRY-RUN] canonical=%s", signed_req.canonical_string)
                    logger.info(
                        "[HMAC DRY-RUN] headers=%s",
                        json.dumps(safe_headers, ensure_ascii=False),
                    )
                    return {
                        "dry_run": True,
                        "path": path,
                        "canonical_string": signed_req.canonical_string,
                        "headers": safe_headers,
                        "payload": payload,
                    }

            logger.debug(f"POST {url} (timeout={timeout}s)")
            response = requests.post(
                url,
                data=body_string,
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            result = response.json() if response.text else {}
            logger.debug(f"POST {url} -> HTTP {response.status_code}")
            return result

        except requests.exceptions.HTTPError as e:
            status_code = getattr(e.response, "status_code", None)
            if signed_req is not None and status_code in {401, 403}:
                logger.warning(
                    "HMAC rejeitado (status=%s path=%s timestamp=%s nonce=%s body_sha256=%s signature=%s)",
                    status_code,
                    path,
                    signed_req.timestamp,
                    signed_req.nonce,
                    signed_req.body_sha256,
                    truncate_signature(signed_req.signature),
                )
            try:
                body = e.response.text
            except Exception:
                body = ""
            error_msg = f"HTTP {e.response.status_code} ao POST {url}: {body}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

        except requests.exceptions.RequestException as e:
            error_msg = f"Erro de rede ao POST {url}: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

        except Exception as e:
            error_msg = f"Erro inesperado ao POST {url}: {e}"
            logger.exception(error_msg)
            raise RuntimeError(error_msg) from e

    def register_clip_metadados(
        self,
        metadados: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Registra metadados do clipe no backend.

        Args:
            metadados: Dicionário com metadados do clipe (venue_id, duration_sec, etc.)
            timeout: Timeout em segundos (usa default_timeout se None)

        Returns:
            Resposta do backend contendo clip_id, upload_url, etc.

        Raises:
            RuntimeError: Se a API não estiver configurada ou ocorrer erro HTTP
        """
        if not self.api_base:
            raise RuntimeError("API base URL não configurada")
        if not self.client_id:
            raise RuntimeError("CLIENT_ID (ou GN_CLIENT_ID) não configurado")
        if not self.venue_id:
            raise RuntimeError("VENUE_ID (ou GN_VENUE_ID) não configurado")

        required_fields = ("sha256", "meta")
        missing_fields = [field for field in required_fields if field not in metadados]
        if missing_fields:
            raise RuntimeError(
                f"Payload de metadados incompleto; faltando: {', '.join(missing_fields)}"
            )

        url = f"{self.api_base}/api/videos/metadados/client/{self.client_id}/venue/{self.venue_id}"
        headers = {"Content-Type": "application/json"}

        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        logger.info("Enviando registro de metadados ao backend...")

        return self._http_post_json(url, metadados, headers=headers, timeout=timeout)

    def upload_file_to_signed_url(
        self,
        upload_url: str,
        file_path: Path,
        content_type: str = "video/mp4",
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: float = 180.0,
    ) -> Tuple[int, str, Dict[str, str]]:
        """
        Envia arquivo via HTTP PUT para URL assinada (S3/Supabase/etc).

        Args:
            upload_url: URL assinada para upload
            file_path: Caminho do arquivo a enviar
            content_type: MIME type do arquivo
            extra_headers: Headers HTTP adicionais
            timeout: Timeout em segundos

        Returns:
            Tupla (status_code, reason, response_headers)

        Raises:
            RuntimeError: Em caso de erro de rede ou I/O
        """
        try:
            logger.info(f"Iniciando upload de {file_path.name} ({file_path.stat().st_size / (1024*1024):.2f} MB)")

            with open(file_path, "rb") as f:
                headers = {"Content-Type": content_type}
                if extra_headers:
                    headers.update(extra_headers)

                response = requests.put(upload_url, data=f, headers=headers, timeout=timeout)

                logger.info(
                    f"Upload concluído: HTTP {response.status_code} {response.reason}"
                )

                normalized_headers = {
                    str(key).strip().lower(): value
                    for key, value in dict(response.headers).items()
                    if key is not None
                }

                return response.status_code, response.reason, normalized_headers

        except requests.exceptions.RequestException as e:
            error_msg = f"Erro de rede durante upload para {upload_url}: {e}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

        except Exception as e:
            error_msg = f"Erro inesperado durante upload: {e}"
            logger.exception(error_msg)
            raise RuntimeError(error_msg) from e

    def finalize_clip_uploaded(
        self,
        clip_id: str,
        size_bytes: int,
        sha256: str,
        etag: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Notifica backend que o upload foi concluído e valida integridade.

        Args:
            clip_id: ID do clipe retornado pelo registro
            size_bytes: Tamanho do arquivo enviado (bytes)
            sha256: Hash SHA-256 do arquivo
            etag: ETag retornado pelo storage (opcional)
            timeout: Timeout em segundos (usa default_timeout se None)

        Returns:
            Resposta do backend confirmando finalização

        Raises:
            RuntimeError: Se a API não estiver configurada ou ocorrer erro HTTP
        """
        if not self.api_base:
            raise RuntimeError("API base URL não configurada")
        if not self.client_id:
            raise RuntimeError("CLIENT_ID (ou GN_CLIENT_ID) não configurado")
        if not str(sha256).strip():
            raise RuntimeError("sha256 inválido para finalização de upload")

        url = f"{self.api_base}/api/videos/{clip_id}/uploaded"
        headers = {"Content-Type": "application/json"}

        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        payload: Dict[str, Any] = {
            "size_bytes": int(size_bytes),
            "sha256": str(sha256),
        }
        if etag:
            payload["etag"] = etag

        logger.info(f"Notificando backend sobre upload concluído (clip_id={clip_id})")
        return self._http_post_json(url, payload, headers=headers, timeout=timeout)
