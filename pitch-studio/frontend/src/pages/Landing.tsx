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
      <section className="relative mx-auto flex min-h-[calc(100vh-4rem)] max-w-7xl flex-col justify-center px-6 py-14 md:px-10 lg:py-20">
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
      </section>
    </div>
  );
}
