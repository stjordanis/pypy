#include <string.h>
#include <stddef.h>
#include <stdlib.h>

#include <stdio.h>
#ifndef _WIN32
#include <unistd.h>
#include <time.h>
#include <sys/time.h>
#else
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#endif
#include "common_header.h"
#include "src/profiling.h"
#include "src/debug_print.h"

__thread_if_stm long pypy_have_debug_prints = -1;
FILE *pypy_debug_file = NULL;   /* XXX make it thread-local too? */
static unsigned char debug_ready = 0;
static unsigned char debug_profile = 0;
__thread_if_stm static char debug_start_colors_1[32];
__thread_if_stm static char debug_start_colors_2[28];
__thread_if_stm char pypy_debug_threadid[16] = {0};
static char *debug_stop_colors = "";
static char *debug_prefix = NULL;
static char *debug_filename = NULL;
static char *debug_filename_with_fork = NULL;

static void pypy_debug_open(void)
{
  char *filename = getenv("PYPYLOG");
  if (filename && filename[0])
    {
      char *colon = strchr(filename, ':');
      if (!colon)
        {
          /* PYPYLOG=filename --- profiling version */
          debug_profile = 1;
          pypy_setup_profiling();
        }
      else
        {
          /* PYPYLOG=prefix:filename --- conditional logging */
          int n = colon - filename;
          debug_prefix = malloc(n + 1);
          memcpy(debug_prefix, filename, n);
          debug_prefix[n] = '\0';
          filename = colon + 1;
        }
      if (strcmp(filename, "-") != 0)
        {
          debug_filename = strdup(filename);
          pypy_debug_file = fopen(filename, "w");
        }
    }
  if (!pypy_debug_file)
    {
      pypy_debug_file = stderr;
      if (isatty(2))
        {
          debug_stop_colors = "\033[0m";
        }
    }
  if (filename)
#ifndef _WIN32
    unsetenv("PYPYLOG");   /* don't pass it to subprocesses */
#else
    putenv("PYPYLOG=");    /* don't pass it to subprocesses */
#endif
  debug_ready = 1;
}

long pypy_debug_offset(void)
{
  if (!debug_ready)
    return -1;
  // note that we deliberately ignore errno, since -1 is fine
  // in case this is not a real file
  fflush(pypy_debug_file);
  return ftell(pypy_debug_file);
}

void pypy_debug_ensure_opened(void)
{
  if (!debug_ready)
    pypy_debug_open();
}

void pypy_debug_forked(long original_offset)
{
  if (debug_filename != NULL)
    {
      char *filename = malloc(strlen(debug_filename) + 32);
      fclose(pypy_debug_file);
      pypy_debug_file = NULL;
      if (filename == NULL)
        return;   /* bah */
      sprintf(filename, "%s.fork%ld", debug_filename, (long)getpid());
      pypy_debug_file = fopen(filename, "w");
      if (pypy_debug_file)
        fprintf(pypy_debug_file, "FORKED: %ld %s\n", original_offset,
                debug_filename_with_fork ? debug_filename_with_fork
                                         : debug_filename);
      free(debug_filename_with_fork);
      debug_filename_with_fork = filename;
    }
}


#ifndef _WIN32

     long long pypy_read_timestamp(void)
     {
#  ifdef CLOCK_THREAD_CPUTIME_ID
       struct timespec tspec;
       clock_gettime(CLOCK_THREAD_CPUTIME_ID, &tspec);
       return ((long long)tspec.tv_sec) * 1000000000LL + tspec.tv_nsec;
#  else
       /* argh, we don't seem to have clock_gettime().  Bad OS. */
       struct timeval tv;
       gettimeofday(&tv, NULL);
       return ((long long)tv.tv_sec) * 1000000LL + tv.tv_usec;
#  endif
     }
#endif


static unsigned char startswithoneof(const char *str, const char *substr)
{
  const char *p = str;
  for (; *substr; substr++)
    {
      if (*substr != ',')
        {
          if (p && *p++ != *substr)
            p = NULL;   /* mismatch */
        }
      else if (p != NULL)
        return 1;   /* match */
      else
        p = str;    /* mismatched, retry with the next */
    }
  return p != NULL;
}

#if defined(_MSC_VER) || defined(__MINGW32__)
#define PYPY_LONG_LONG_PRINTF_FORMAT "I64"
#else
#define PYPY_LONG_LONG_PRINTF_FORMAT "ll"
#endif

static void display_startstop(const char *prefix, const char *postfix,
                              const char *category, const char *colors)
{
  long long timestamp;
  READ_TIMESTAMP(timestamp);
  fprintf(pypy_debug_file, "%s[%"PYPY_LONG_LONG_PRINTF_FORMAT"x] %s%s%s\n%s",
          colors,
          timestamp, prefix, category, postfix,
          debug_stop_colors);
}

#ifdef RPY_STM
# include <src_stm/atomic_ops.h>
# include <src_stm/fprintcolor.h>
#else
  typedef long revision_t;
# define bool_cas(vp, o, n) (*(vp)=(n), 1)
# define dprintfcolor() 0
#endif
static revision_t threadcounter = 0;

static void _prepare_display_colors(void)
{
    revision_t counter;
    char *p;
    while (1) {
        counter = threadcounter;
        if (bool_cas(&threadcounter, counter, counter + 1))
            break;
    }
    if (debug_stop_colors[0] == 0) {
        /* not a tty output: no colors */
        sprintf(debug_start_colors_1, "%d# ", (int)counter);
        sprintf(debug_start_colors_2, "%d# ", (int)counter);
#ifdef RPY_STM
        sprintf(pypy_debug_threadid, "%d#", (int)counter);
#endif
    }
    else {
        /* tty output */
        /* If we have STM and it is compiled in debug mode, share
           the color to use.  It avoids endless confusion caused by
           each thread using two independent colors for the two
           debugging outputs. */
        int color = dprintfcolor();
        if (color == 0) {
            color = 31 + (int)(counter % 7);
        }
        sprintf(debug_start_colors_1, "\033[%dm%d# \033[1m",
                color, (int)counter);
        sprintf(debug_start_colors_2, "\033[%dm%d# ",
                color, (int)counter);
#ifdef RPY_STM
        sprintf(pypy_debug_threadid, "\033[%dm%d#\033[0m",
                color, (int)counter);
#endif
    }
}

void pypy_debug_start(const char *category)
{
  pypy_debug_ensure_opened();
  /* Enter a nesting level.  Nested debug_prints are disabled by default
     because the following left shift introduces a 0 in the last bit.
     Note that this logic assumes that we are never going to nest
     debug_starts more than 31 levels (63 on 64-bits). */
  pypy_have_debug_prints <<= 1;
  if (!debug_profile)
    {
      /* non-profiling version */
      if (!debug_prefix || !startswithoneof(category, debug_prefix))
        {
          /* wrong section name, or no PYPYLOG at all, skip it */
          return;
        }
      /* else make this subsection active */
      pypy_have_debug_prints |= 1;
    }
  if (!debug_start_colors_1[0])
    _prepare_display_colors();
  display_startstop("{", "", category, debug_start_colors_1);
}

void pypy_debug_stop(const char *category)
{
  if (debug_profile | (pypy_have_debug_prints & 1))
    display_startstop("", "}", category, debug_start_colors_2);
  pypy_have_debug_prints >>= 1;
}
