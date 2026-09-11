/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        black: "#0B0B0F",
        offwhite: "#F5F2EB",
        grey: {
          DEFAULT: "#6B6B76",
          light: "#E8E4DB",
          dark: "#3A3A42",
        },
        "brand-yellow": "#F5C518",
        /* Compatibility aliases used by existing admin pages */
        ink: "#0B0B0F",
        paper: "#F5F2EB",
        surface: "#FFFFFF",
        muted: "#6B6B76",
        line: "#E8E4DB",
        accent: "#0B0B0F",
        "accent-dark": "#000000",
        gold: "#F5C518",
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
        card: "0 12px 40px -24px rgba(11, 11, 15, 0.28)",
        glass: "0 8px 32px -12px rgba(11, 11, 15, 0.18)",
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
