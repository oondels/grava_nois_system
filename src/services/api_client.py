"""
Cliente HTTP para interação com a API do backend Grava Nois.

Centraliza todas as chamadas HTTP (registro de metadados, upload para URL assinada,
finalização de upload) em uma classe reutilizável.
"""

import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

from src.utils.logger import logger


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
        self.default_timeout = default_timeout

        if not self.api_base:
            logger.warning("API base URL não configurada (GN_API_BASE ou API_BASE_URL)")

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
        headers = headers or {}

        try:
            logger.debug(f"POST {url} (timeout={timeout}s)")
            response = requests.post(url, json=payload, headers=headers, timeout=timeout)
            response.raise_for_status()
            result = response.json() if response.text else {}
            logger.debug(f"POST {url} -> HTTP {response.status_code}")
            return result

        except requests.exceptions.HTTPError as e:
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

                return response.status_code, response.reason, dict(response.headers)

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
