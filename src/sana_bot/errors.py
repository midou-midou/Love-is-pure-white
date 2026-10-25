"""统一错误类型与退出码。

退出码：0 成功、2 输入或配置错误、3 资料/校准未就绪、4 外部服务失败。
所有错误带 ``reason_code``，可带 ``correlation_id`` 便于关联诊断记录。
"""

from __future__ import annotations

from typing import Any

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_NOT_READY = 3
EXIT_EXTERNAL_FAILURE = 4


class SanaError(Exception):
    """所有可预期失败的基类；不得用于程序内部缺陷。"""

    exit_code = EXIT_EXTERNAL_FAILURE
    reason_code = "unexpected_failure"

    def __init__(
        self,
        message: str,
        *,
        reason_code: str | None = None,
        correlation_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if reason_code is not None:
            self.reason_code = reason_code
        self.correlation_id = correlation_id
        self.details = details or {}


class ConfigError(SanaError):
    """输入参数、配置文件或环境配置非法。"""

    exit_code = EXIT_CONFIG_ERROR
    reason_code = "config_invalid"


class NotReadyError(SanaError):
    """资料、结构、校准或索引尚未就绪，功能不能启用。"""

    exit_code = EXIT_NOT_READY
    reason_code = "not_ready"


class ExternalServiceError(SanaError):
    """模型、QQ 平台或网络等外部依赖失败。"""

    exit_code = EXIT_EXTERNAL_FAILURE
    reason_code = "external_service_failure"


class FeatureNotImplemented(SanaError):
    """明确未实现的功能；不允许返回伪成功。"""

    exit_code = EXIT_CONFIG_ERROR
    reason_code = "not_implemented"


# --------------------------------------------------------------------------- 资料包


class PackageFailure(ConfigError):
    """资料包、路径、字段或容量问题；默认按输入错误退出。"""

    exit_code = EXIT_CONFIG_ERROR
    reason_code = "package_invalid"


class PackageNotReady(NotReadyError):
    """资料包完整性、绑定或验收未通过；不得启用。"""

    exit_code = EXIT_NOT_READY
    reason_code = "package_not_ready"


class PackageExternalFailure(ExternalServiceError):
    """编码器、聊天模型或平台等外部依赖失败。"""

    exit_code = EXIT_EXTERNAL_FAILURE
    reason_code = "package_external_failure"


#: 资料包相关 reason_code → 退出码；未登记的一律按外部失败处理
REASON_EXIT_CODES: dict[str, int] = {
    # 输入/配置错误（2）
    "package_file_missing": EXIT_CONFIG_ERROR,
    "package_metadata_invalid": EXIT_CONFIG_ERROR,
    "repeated_metadata_field": EXIT_CONFIG_ERROR,
    "duplicate_card_id": EXIT_CONFIG_ERROR,
    "unknown_card_type": EXIT_CONFIG_ERROR,
    "package_scope_unmapped": EXIT_CONFIG_ERROR,
    "source_path_escape": EXIT_CONFIG_ERROR,
    "example_speaker_ambiguous": EXIT_CONFIG_ERROR,
    "unknown_index_format": EXIT_CONFIG_ERROR,
    "split_profile_required": EXIT_CONFIG_ERROR,
    "split_profile_forbidden": EXIT_CONFIG_ERROR,
    "invalid_report_mode": EXIT_CONFIG_ERROR,
    # 未就绪/未验收（3）
    "resident_missing": EXIT_NOT_READY,
    "package_count_mismatch": EXIT_NOT_READY,
    "card_input_over_limit": EXIT_NOT_READY,
    "tokenizer_unverified": EXIT_NOT_READY,
    "bundle_hash_mismatch": EXIT_NOT_READY,
    "package_unavailable": EXIT_NOT_READY,
    "package_relationship_mismatch": EXIT_NOT_READY,
    "package_calibration_missing": EXIT_NOT_READY,
    "package_report_missing": EXIT_NOT_READY,
    "package_report_not_approved": EXIT_NOT_READY,
    "report_version_mismatch": EXIT_NOT_READY,
    "report_bundle_mismatch": EXIT_NOT_READY,
    "active_version_conflict": EXIT_NOT_READY,
    "index_not_reviewed": EXIT_NOT_READY,
    "monologue_material_absent": EXIT_NOT_READY,
    "context_over_limit": EXIT_NOT_READY,
    "package_offline_only": EXIT_NOT_READY,
    "evaluation_incomplete": EXIT_NOT_READY,
    "index_artifact_missing": EXIT_NOT_READY,
    # 外部/处理异常（4）
    "package_encoder_failure": EXIT_EXTERNAL_FAILURE,
    "embedding_failed": EXIT_EXTERNAL_FAILURE,
    "embedding_non_finite": EXIT_EXTERNAL_FAILURE,
    "embedding_dimension_mismatch": EXIT_EXTERNAL_FAILURE,
    "model_failure": EXIT_EXTERNAL_FAILURE,
}

_SENSITIVE_KEYS = ("key", "secret", "token", "password", "authorization", "credential")
_MAX_DETAIL_TEXT = 200


def exit_code_for(reason_code: str, *, default: int = EXIT_EXTERNAL_FAILURE) -> int:
    return REASON_EXIT_CODES.get(reason_code, default)


def redact_details(details: Any) -> Any:
    """脱敏：去掉凭据类字段，截断过长文本，不返回密钥或完整提示词。"""
    if isinstance(details, dict):
        redacted: dict[str, Any] = {}
        for key, value in details.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in _SENSITIVE_KEYS):
                redacted[key] = "[redacted]"
                continue
            redacted[key] = redact_details(value)
        return redacted
    if isinstance(details, (list, tuple)):
        return [redact_details(item) for item in details]
    if isinstance(details, str) and len(details) > _MAX_DETAIL_TEXT:
        return details[:_MAX_DETAIL_TEXT] + "…"
    return details
