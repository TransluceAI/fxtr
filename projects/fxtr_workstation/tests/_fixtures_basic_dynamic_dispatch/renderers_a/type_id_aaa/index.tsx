// Smoke fixture: a renderer for a fake type whose id_string is
// "type_id_aaa". The compiled module's default export is a 2-level
// map `[subtype_key][dispatch_key_uuid] -> Component`.
//
// We deliberately import from one of the external set (`react/jsx-runtime`)
// so the test can confirm the import is preserved as a bare specifier
// in the compiled ESM bytes rather than bundled in.
import { jsx as _jsx } from "react/jsx-runtime";

const FAKE_DISPATCH_KEY_UUID = "fake-dispatch-key-uuid-aaa";

function AaaCard(props: { stub: unknown }) {
	return _jsx("div", { children: "aaa-card" });
}

export default {
	default: {
		[FAKE_DISPATCH_KEY_UUID]: AaaCard,
	},
};
