import { describe, expect, it } from "vitest";

import type { ServiceDescriptor } from "../src/descriptor";
import { MissingServiceTypeError, buildServiceMetadata } from "../src/metadata";

describe("buildServiceMetadata", () => {
	it("reads type_id_string from the descriptor's service_type_id", () => {
		const desc: ServiceDescriptor = {
			kind: "http",
			url: "/svc/echo",
			method: "GET",
			service_type_id: "!some-type",
		};
		expect(buildServiceMetadata(desc)).toEqual({ type_id_string: "!some-type" });
	});

	it("throws MissingServiceTypeError when service_type_id is absent", () => {
		const desc: ServiceDescriptor = { kind: "http", url: "/svc/echo", method: "GET" };
		expect(() => buildServiceMetadata(desc)).toThrow(MissingServiceTypeError);
	});
});
