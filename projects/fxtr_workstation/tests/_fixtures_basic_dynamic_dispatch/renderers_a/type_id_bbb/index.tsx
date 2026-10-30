// Second smoke fixture: renderer for a fake type id_string
// "type_id_bbb". Same shape as type_id_aaa/index.tsx; together they
// exercise the dir-walking + multi-bucket compile loop.
import { jsx as _jsx } from "react/jsx-runtime";

const FAKE_DISPATCH_KEY_UUID = "fake-dispatch-key-uuid-bbb";

function BbbCard() {
	return _jsx("div", { children: "bbb-card" });
}

export default {
	default: {
		[FAKE_DISPATCH_KEY_UUID]: BbbCard,
	},
};
