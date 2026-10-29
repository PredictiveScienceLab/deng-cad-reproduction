"""Three-tool allowlist using the unchanged v2 transport validation/compiler."""
import copy
from broker_v3 import V2, CHECK_DESCRIPTION
from tool_gate import Gate, DEFAULT_TOOLS, make_server, response_sse, MODEL

TOOLS_V3 = copy.deepcopy(DEFAULT_TOOLS)
TOOLS_V3['check_geometry'] = {'backend': 'mcp__deng_cad__check_geometry',
    'description': CHECK_DESCRIPTION,
    'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}


class GateV3(Gate):
    def __init__(self, audit=None):
        super().__init__(TOOLS_V3, audit)

    def request(self, original):
        rewritten = super().request(original)
        for item in rewritten['input']:
            if item.get('type') == 'function_call_output':
                # CLI custom-output IDs use ctco_*. They are not valid IDs for
                # a Responses function output. The optional ID can be omitted;
                # call_id retains the binding already verified by the v2 gate.
                item.pop('id', None)
        return rewritten
