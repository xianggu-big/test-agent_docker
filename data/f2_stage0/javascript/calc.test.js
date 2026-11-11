const { add, div, clamp } = require("./src/calc");
test("add", () => { expect(add(1, 2)).toBe(3); });
test("div", () => { expect(div(6, 3)).toBe(2); });
test("clamp 边界", () => { expect(clamp(90, 60, 90)).toBe(90); });
