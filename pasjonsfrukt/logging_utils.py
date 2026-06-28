import logging


class LogRedactSecretFilter(logging.Filter):

    def __init__(self, secrets: list[str], redact_string: str = "******"):
        super().__init__()
        self.secrets = [s for s in secrets if s]  # fix #5: skip empty secrets
        self.redact_string = redact_string

    def _redacted_string(self, s) -> str:
        redacted = s
        for secret in self.secrets:
            redacted = redacted.replace(secret, self.redact_string)
        return redacted

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redacted_string(record.msg)
        if isinstance(record.args, dict):  # fix #2: handle dict args
            record.args = {
                k: self._redacted_string(v) if isinstance(v, str) else v
                for k, v in record.args.items()
            }
        elif record.args:  # fix #2: handle None args
            record.args = tuple(
                self._redacted_string(a) if isinstance(a, str) else a
                for a in record.args
            )
        return True
