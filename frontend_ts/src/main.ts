export function greet(name: string): string {
  return `Hello, ${name}!`;
}

const app = document.getElementById("app");
if (app) {
  app.textContent = greet("Antigona");
}
