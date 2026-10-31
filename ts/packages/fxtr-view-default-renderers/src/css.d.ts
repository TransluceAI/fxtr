// Renderer packages ship stylesheets this module imports for their side effect; the bundler
// resolves them and TypeScript needs only to know they carry no exports.
declare module "*.css";
