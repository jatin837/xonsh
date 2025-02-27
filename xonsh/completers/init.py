"""Constructor for xonsh completer objects."""
import collections

from xonsh.completers.man import complete_from_man
from xonsh.completers.bash import complete_from_bash
from xonsh.completers.base import complete_base
from xonsh.completers.path import complete_path
from xonsh.completers.python import (
    complete_python,
)
from xonsh.completers.imports import complete_import
from xonsh.completers.commands import (
    complete_skipper,
    complete_end_proc_tokens,
    complete_end_proc_keywords,
    complete_xompletions,
)
from xonsh.completers._aliases import complete_aliases
from xonsh.completers.environment import complete_environment_vars


def default_completers():
    """Creates a copy of the default completers."""
    return collections.OrderedDict(
        [
            # non-exclusive completers:
            ("end_proc_tokens", complete_end_proc_tokens),
            ("end_proc_keywords", complete_end_proc_keywords),
            ("environment_vars", complete_environment_vars),
            # exclusive completers:
            ("base", complete_base),
            ("skip", complete_skipper),
            ("alias", complete_aliases),
            ("xompleter", complete_xompletions),
            ("import", complete_import),
            ("bash", complete_from_bash),
            ("man", complete_from_man),
            ("python", complete_python),
            ("path", complete_path),
        ]
    )
