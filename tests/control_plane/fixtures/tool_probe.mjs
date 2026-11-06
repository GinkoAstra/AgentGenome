const { default: register } = await import(process.argv[2]);
const tools = [];
register({ registerTool: (tool) => tools.push(tool) });
const tool = tools.find((item) => item.name === process.argv[3]);
if (!tool) {
  process.stdout.write(JSON.stringify({ probe_error: "unknown_tool" }));
  process.exit(2);
}
const result = await tool.execute("probe-call", JSON.parse(process.argv[4]));
process.stdout.write(JSON.stringify(result));
