// CSS Modules imports (`import styles from "./x.module.css"`), for every package extending
// tsconfig.base.json: the local class names, each mapped to the name the build gives it.
declare module "*.module.css" {
  const classes: { readonly [local: string]: string };
  export default classes;
}
