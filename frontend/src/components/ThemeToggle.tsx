import { useState } from 'react'
import { Monitor, Moon, Sun, type LucideIcon } from 'lucide-react'
import { currentTheme, setTheme, type Theme } from '../lib/theme'
import styles from './ThemeToggle.module.css'

const CHOICES: readonly { theme: Theme; label: string; Icon: LucideIcon }[] = [
  { theme: 'system', label: 'Follow the system theme', Icon: Monitor },
  { theme: 'light', label: 'Light theme', Icon: Sun },
  { theme: 'dark', label: 'Dark theme', Icon: Moon },
]

/** At the foot of the rail: follow the system's light or dark preference, or pick one. */
export function ThemeToggle() {
  const [theme, setShown] = useState(currentTheme)
  return (
    <div className={styles.row}>
      <span>theme</span>
      <div className={styles.choices} role="group" aria-label="Theme">
        {CHOICES.map(({ theme: choice, label, Icon }) => (
          <button
            key={choice}
            className="icon-button"
            aria-pressed={theme === choice}
            aria-label={label}
            title={label}
            onClick={() => {
              setTheme(choice)
              setShown(choice)
            }}
          >
            <Icon size={12} />
          </button>
        ))}
      </div>
    </div>
  )
}
