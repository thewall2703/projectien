/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#14141A",
        paper: "#F5F2EB",
        surface: "#FFFFFF",
        accent: "#2439D9",
        "accent-dark": "#16227A",
        gold: "#C9A24B",
        muted: "#6B6B76",
        line: "#E7E2D8",
        success: "#1B8A5A",
        warn: "#B7791F",
        danger: "#B4232A",
      },
      fontFamily: {
        display: ["Fraunces Variable", "Fraunces", "Georgia", "Times New Roman", "serif"],
        sans: ["Inter Variable", "Inter", "Helvetica Neue", "Arial", "sans-serif"],
        serif: ["Fraunces Variable", "Fraunces", "Georgia", "Times New Roman", "serif"],
      },
      boxShadow: {
        card: "0 12px 40px -24px rgba(20, 20, 26, 0.35)",
      },
      borderRadius: {
        xl: "1rem",
        "2xl": "1.25rem",
      },
      keyframes: {
        fadeIn: {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        slideUp: {
          "0%": { opacity: "0", transform: "translateY(8px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        "fade-in": "fadeIn 280ms ease-out",
        "slide-up": "slideUp 320ms ease-out",
      },
    },
  },
  plugins: [],
};
