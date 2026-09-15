import { motion } from "framer-motion";
import { Link } from "react-router-dom";
import { Button } from "../components/ui";

export default function Landing() {
  return (
    <div className="relative overflow-hidden">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0"
        style={{
          background:
            "radial-gradient(ellipse 80% 50% at 50% -10%, rgba(245,197,24,0.12), transparent 55%), radial-gradient(ellipse 60% 40% at 90% 20%, rgba(11,11,15,0.04), transparent 50%)",
        }}
      />
      <section className="relative mx-auto grid min-h-[calc(100vh-4rem)] max-w-7xl items-center gap-12 px-6 py-14 md:px-10 lg:grid-cols-[0.9fr_1.1fr] lg:gap-16 lg:py-20">
        <div>
          <motion.p
            className="kicker"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4 }}
          >
            Masters&apos; Union
          </motion.p>
          <motion.h1
            className="mt-5 max-w-3xl font-display text-5xl leading-[1.05] tracking-tight text-black md:text-7xl"
            initial={{ opacity: 0, y: 16 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.55, delay: 0.05 }}
          >
            One company.
            <br />
            One story.
            <br />
            Every pitch.
          </motion.h1>
          <motion.p
            className="mt-6 max-w-xl text-base leading-relaxed text-grey md:text-lg"
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.45, delay: 0.15 }}
          >
            Build a script, deck, and evidence pack from the One Company matrix — tuned to audience,
            channel, and intent.
          </motion.p>
          <motion.div
            className="mt-10"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.4, delay: 0.25 }}
          >
            <Link to="/generate">
              <Button variant="accent" className="px-8 py-3 text-base">
                Generate
              </Button>
            </Link>
          </motion.div>
        </div>

        <motion.div
          className="glass-panel relative mx-auto aspect-square w-full max-w-[39rem] overflow-hidden p-2.5"
          initial={{ opacity: 0, scale: 0.96, x: 24 }}
          animate={{ opacity: 1, scale: 1, x: 0 }}
          transition={{ duration: 0.75, delay: 0.12, ease: [0.22, 1, 0.36, 1] }}
        >
          <div
            aria-hidden
            className="pointer-events-none absolute inset-0 z-10 rounded-[inherit] bg-[radial-gradient(circle_at_25%_8%,rgba(255,255,255,0.24),transparent_32%),linear-gradient(135deg,rgba(255,255,255,0.13),transparent_42%)]"
          />
          <img
            src="/pitch-studio-iridescent-art.png"
            alt="Iridescent abstract sculptural artwork"
            className="h-full w-full rounded-[1rem] object-cover"
          />
        </motion.div>
      </section>
    </div>
  );
}
