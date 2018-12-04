# Tumblr Backup 101

This guide is for 100% programming/coding newbies to use this Tumblr Backup service.

## Why 101?
Tumblr does not have an export service, and all the easily downloadable/online ones are not very good. I strongly believe that any service you use should make it easy to back up your words and work.

This program backs up your Tumblr onto your computer, and saves it on your hard drive. It ends up looking like [this](http://drbeat.li/tumblr). This program is excellent and easy to use - but also a but intimidating if you have never used command line programmes before.

Don't panic! I'm going to walk you through step by step.

## Getting Started
This guide is for Windows users. 

### Step 1: Install Python
1. The program we are going to run is called tumblr_backup.py. It is a **python file**. This means it is a file written in the programming language Python.

2. Just like you need a program like Word to view a_document.doc, or Paint to view a_picture.jpeg, you need to download Python to make this program work.

3. Go to the [Python website](https://www.python.org/downloads/release/python-2712/). We are downloading v.2 of Python because this program is designed to work with v2

4. Download the file called **Windows x86 MSI installer**

5. Install it by double clicking.

6. You've installed Python! You can now run Python programs, and if you want to learn to code, you can also use this installation to practice your coding.

### Step 2: Download tumblr_backup

1. Download and unzip this file: [tumblr-utils.zip](https://github.com/bbolli/tumblr-utils/zipball/master)

2. Unzip the file somewhere easy to find, say in your Downloads folder. 

3. We are now going to add this file to your $PATH. What is $PATH? It essentially tells the computer how to find certain things when it needs to use them. 

4. First, you need to find the path of the folder your download is in. A path is like a url. Mine looks like:
`C:\Users\Unmutual\Downloads\bbolli-tumblr-utils-3a37fe6\bbolli-tumblr-utils-3a37fe6`
(Yours will be different. The word Unmutual is my username; and you may have saved your file in a different place)

5. Open up Control Panel. Search for Advanced System Settings. Click the link reading Environment Variables. 

6. Scroll down the variables until you find one reading "Path". Click it. Click edit. If there is nothing in the box, simply paste in the url. If there is something in the box add a semi-colon to the end of the line. Then, paste in the url. (the semi-colon tells the computer to treat the two things as different, not interpret it as one big thing)

(I learnt how to do this from [this page](https://www.java.com/en/download/help/path.xml), which gives lots of options for different windows systems. Check the link if my description isn't working for you.)

### Step 3. Use the Command Line

1. The command line is the bit of the computer which makes you feel like you're in the Matrix. Once you get used to the command line, you will become fucking addicted to it - I promise. This is because the command line is like seeing the puppeteer beind the puppet show. You will feel powerful. You will feel like the computer is yours to control, not this arcane box, but *your* computer which you can use to do pretty much anything.

2. To find the command line, go to your system search and type in "Command Prompt". Click it.

3. Your next step is to navigate the prompt to the file tumblr_backup.py. There are better guides out there than this for using the command prompt. I am going to explain, but feel free to google for one with pictures.

4. On the left hand side of the screen is part of a Path. For me, it reads `C:\Users\Unmutual>`, and then there is a blinky cursor. 

5. Type `cd Downloads` and then press enter. Your screen now reads `C:\Users\Unmutual\Downloads>` (with your name, in place of the word "Unmutual"). "cd" stands for "change directory". You have gone one directory down! This is equivalent to just double clicking on the downloads folder. If you go wrong, typing `cd ..` will go up one directory again (back to C:\Users\Unmutual>). Have a play around and do some cackling. If you simply type "dir" it will give you a list of all the files in that directory.

6. Once you're done pretending to be in the Matrix, navigate to the folder the file tumblr_backup.py is in. For me, this is:
`cd C:\Users\Unmutual\Downloads\bbolli-tumblr-utils-3a37fe6`, or, from the Downloads folder, just `cd bbolli-tumblr-utils-3a37fe6`.

### Step 4. Run!

1. Plug in your laptop charger, and make sure you have a stable internet connection, and that the laptop won't auto shutdown, sleep or screensaver. This program will run for a while and it's a faff to restart.

2. Where the blinky cursor is, type `python tumblr_backup.py yourtumblrname`. The first bit tells the Windows to run Python, the second bit tells Python to run the backup script, and the third bit - yourtumblrname - tells the backup script which tumblr to download. For example, you may type

    python tumblr_backup.py discoinferno

If you are tumblr user @ discoinferno. 

(You can use this to backup any tumblr, including someone else's, but I think that's a tad shady)

3. Your command prompt will start spitting letters and phrases onto the screen. Leave it to it! You can do other stuff while you wait, just leave the black command prompt box open and running.

### Step 5. How tumblr_backup works

tumblr_backup grabs 50 posts at a time and downloads them onto your hard drive. In the same folder as the program tumblr_backup.py, it will create a folder with the name of your blog. it downloads everything into the folder. 

Once you're done, you can open the folder and find the document called "index.html". Right click index.html, and choose "Open With Firefox" - or whatever internet browser you use.

### HOW TO USE FLAGS

In Step 3, you use the command line to tell the program to run.

You type in the name of the program, and then your username.

You can also add "flags" which give the program special running instructions.

You put flags **between** the program name and username - for example:

    tumblr_backup.py -t DOGS discoinferno

Would only backup pages marked "dogs". You can see the whole list of flags in tumblr_backup.md.

They are useful for, example - using `-T text` to only download your text posts, or `-p 2018` to only download this year's posts.


###### TODO

1. how to restart the process
2. more detail
3. probably links and pictures




