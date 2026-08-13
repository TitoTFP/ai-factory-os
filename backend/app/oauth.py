from __future__ import annotations

from typing import Any

import httpx

from .config import Settings


class OAuthError(RuntimeError):
    """The provider did not return a verified OAuth identity."""


async def verify_oauth_code(provider: str, code: str, redirect_uri: str, settings: Settings) -> dict[str, str]:
    if provider == "github":
        return await _github_profile(code, redirect_uri, settings)
    if provider == "google":
        return await _google_profile(code, redirect_uri, settings)
    raise OAuthError("unsupported OAuth provider")


async def _github_profile(code: str, redirect_uri: str, settings: Settings) -> dict[str, str]:
    if not settings.oauth_github_client_id or not settings.oauth_github_client_secret:
        raise OAuthError("GitHub OAuth is not configured")
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
        try:
            token_response = await client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": settings.oauth_github_client_id,
                    "client_secret": settings.oauth_github_client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                },
                headers={"Accept": "application/json"},
            )
            token_response.raise_for_status()
            token_data: Any = token_response.json()
            access_token = token_data.get("access_token") if isinstance(token_data, dict) else None
            if not access_token:
                raise OAuthError("GitHub did not issue an access token")
            headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/vnd.github+json"}
            profile_response = await client.get("https://api.github.com/user", headers=headers)
            profile_response.raise_for_status()
            profile: Any = profile_response.json()
            email_response = await client.get("https://api.github.com/user/emails", headers=headers)
            email_response.raise_for_status()
            emails: Any = email_response.json()
        except httpx.HTTPError as exc:
            raise OAuthError("GitHub OAuth verification failed") from exc
    if not isinstance(profile, dict) or not isinstance(emails, list):
        raise OAuthError("GitHub returned an invalid identity")
    verified = next((item for item in emails if isinstance(item, dict) and item.get("verified") and item.get("primary")), None)
    verified = verified or next((item for item in emails if isinstance(item, dict) and item.get("verified")), None)
    email = verified.get("email") if isinstance(verified, dict) else None
    subject = profile.get("id")
    if not email or not subject:
        raise OAuthError("GitHub account has no verified email")
    return {"subject": str(subject), "email": str(email).strip().lower(), "name": str(profile.get("name") or profile.get("login") or "")}


async def _google_profile(code: str, redirect_uri: str, settings: Settings) -> dict[str, str]:
    if not settings.oauth_google_client_id or not settings.oauth_google_client_secret:
        raise OAuthError("Google OAuth is not configured")
    async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
        try:
            token_response = await client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "client_id": settings.oauth_google_client_id,
                    "client_secret": settings.oauth_google_client_secret,
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            token_response.raise_for_status()
            token_data: Any = token_response.json()
            access_token = token_data.get("access_token") if isinstance(token_data, dict) else None
            if not access_token:
                raise OAuthError("Google did not issue an access token")
            profile_response = await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            profile_response.raise_for_status()
            profile: Any = profile_response.json()
        except httpx.HTTPError as exc:
            raise OAuthError("Google OAuth verification failed") from exc
    if not isinstance(profile, dict) or not profile.get("email_verified"):
        raise OAuthError("Google account has no verified email")
    subject = profile.get("sub")
    email = profile.get("email")
    if not subject or not email:
        raise OAuthError("Google returned an invalid identity")
    return {"subject": str(subject), "email": str(email).strip().lower(), "name": str(profile.get("name") or "")}
