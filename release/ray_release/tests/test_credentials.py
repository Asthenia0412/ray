import json
from unittest.mock import MagicMock, patch

import pytest

from ray_release.credentials import prepare_release_credentials


@pytest.fixture
def config():
    return {
        "role_arn": "arn:aws:iam::959243851260:role/test-role",
        "kubernetes": {
            "server": "https://kubernetes.example.com",
            "namespace": "release-tests",
            "token": "kubernetes-token",
            "token_expires_at": "2099-01-01T00:00:00Z",
        },
        "anyscale_host": "https://console.anyscale-staging.com",
        "anyscale_token": "anyscale-token",
    }


def test_existing_tests_do_not_provision_credentials():
    with patch("ray_release.credentials.boto3.client") as client:
        prepare_release_credentials({"cluster": {}})
    client.assert_not_called()


@pytest.mark.parametrize("status", [201, 409, 403])
def test_create_only_credentials(config, monkeypatch, status):
    for key in (
        "RELEASE_AWS_SECRET_NAME",
        "RELEASE_IMAGE_PULL_SECRET_NAME",
        "RELEASE_ANYSCALE_SECRET_NAME",
    ):
        monkeypatch.delenv(key, raising=False)
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": json.dumps(config)}
    client.assume_role.return_value = {
        "Credentials": {
            "AccessKeyId": "access-key",
            "SecretAccessKey": "secret-key",
            "SessionToken": "session-token",
        }
    }
    ecr = MagicMock()
    ecr.get_authorization_token.return_value = {
        "authorizationData": [
            {
                "proxyEndpoint": "https://registry.example.com",
                "authorizationToken": "auth",
            }
        ]
    }
    with patch("ray_release.credentials.boto3.client", return_value=client), patch(
        "ray_release.credentials.boto3.Session"
    ) as session, patch("ray_release.credentials.requests.post") as post:
        session.return_value.client.return_value = ecr
        post.return_value.status_code = status
        if status != 201:
            with pytest.raises(RuntimeError, match=f"HTTP {status}"):
                prepare_release_credentials(
                    {"cluster": {"credential_config_secret": "arn"}}
                )
            assert post.call_count == 1
            return
        prepare_release_credentials({"cluster": {"credential_config_secret": "arn"}})
        assert post.call_count == 3
        aws_secret, image_secret, api_secret = [
            call.kwargs["json"] for call in post.call_args_list
        ]
        assert api_secret["stringData"] == {
            "token": "anyscale-token",
            "host": "https://console.anyscale-staging.com",
        }
        assert "token" not in aws_secret["stringData"]
        assert "token" not in image_secret["stringData"]
        assert aws_secret["metadata"]["name"] != image_secret["metadata"]["name"]
        assert "[release-tests]" in aws_secret["stringData"]["credentials"]
        docker = json.loads(image_secret["stringData"][".dockerconfigjson"])
        assert docker["auths"]["registry.example.com"]["auth"] == "auth"
        assert all(call.kwargs["verify"] is True for call in post.call_args_list)
        assert all(call.kwargs["timeout"] == 30 for call in post.call_args_list)


def test_expired_kubernetes_identity_fails_before_assuming_role(config):
    config["kubernetes"]["token_expires_at"] = "2000-01-01T00:00:00Z"
    client = MagicMock()
    client.get_secret_value.return_value = {"SecretString": json.dumps(config)}
    with patch("ray_release.credentials.boto3.client", return_value=client):
        with pytest.raises(RuntimeError, match="expired"):
            prepare_release_credentials(
                {"cluster": {"credential_config_secret": "arn"}}
            )
    client.assume_role.assert_not_called()
