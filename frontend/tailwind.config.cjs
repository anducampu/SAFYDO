/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        display: ["Syne", "system-ui", "sans-serif"],
        sans: ["IBM Plex Sans", "system-ui", "sans-serif"],
      },
      colors: {
        ink: "#08080c",
        surface: "#12121a",
        "surface-2": "#1c1c28",
        mist: "#8b8798",
      },
    },
  },
  plugins: [],
};